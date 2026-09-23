/*
 * wave.c —— 第5周：三轴波形的板端采样任务 + 环形缓冲
 *
 * 结构（生产者 / 消费者）：
 *      wave_task（本文件，独立任务，20Hz）    →  环形缓冲  →  main 主循环（攒够一批就 POST）
 *
 * 三个关键取舍，都写在各处注释里：
 *   1. 采样用 vTaskDelayUntil 而不是 vTaskDelay —— 后者是"干完活再等 50ms"，
 *      每轮的实际间隔 = 50ms + 干活耗时，误差会一轮轮累加；
 *      前者是"到点就走"，周期不会被干活耗时带偏。示波器的横轴靠的就是这个周期。
 *   2. 环形缓冲满了丢**最老的**（而不是丢最新的）：示波器要看的是"现在"，
 *      丢新样本等于画面卡住不动，比少一段更难看。丢了就如实计数。
 *   3. 索引用 uint32 自然回绕，`head - tail` 在无符号运算下天然得到"有多少个"，
 *      不需要额外的计数变量（多一个变量就多一处能对不上的地方）。
 */
#include "wave.h"

#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"

#include "qma7981.h"

static const char *TAG = "wave";

/* 环形缓冲容量：必须是 2 的幂（取模用位与，比除法快且不会算错）。
 * 512 个样本 @20Hz = 25.6 秒 —— 这个长度是照着"开机后连 Wi-Fi 可能要等十几秒"
 * 留的余量：那段时间采样任务已经在跑了，缓冲得装得下，否则第一批就带 dropped。
 *
 * 注意这里**不能写类型转换**（`((uint32_t)WAVE_RING_SAMPLES)`）：下面的 #if
 * 是预处理器表达式，预处理器不认识 C 类型转换，会报
 * "missing binary operator before token" —— 而且报错位置指向 app_config.h，
 * 看着像是配置文件写错了。 */
#define RING_SAMPLES   WAVE_RING_SAMPLES
#define RING_MASK      (RING_SAMPLES - 1u)

#if (RING_SAMPLES == 0) || ((RING_SAMPLES & RING_MASK) != 0)
#error "WAVE_RING_SAMPLES 必须是 2 的幂，否则下面的位与取模是错的"
#endif

/* 采样周期：1000ms / WAVE_HZ。20Hz -> 50ms。
 * 注意 configTICK_RATE_HZ 默认 100Hz，50ms 正好是 5 个 tick，不会有取整误差。 */
#define WAVE_PERIOD_MS (1000 / WAVE_HZ)

typedef struct {
    int16_t x, y, z;      /* 原始 ADC 计数（14 位补码，范围 ±8191）*/
    int64_t t_us;         /* 采样时刻（esp_timer 微秒，开机以来的单调时刻）*/
} wave_sample_t;

static wave_sample_t s_ring[RING_SAMPLES];
static volatile uint32_t s_head;        /* 生产者写入位置 */
static volatile uint32_t s_tail;        /* 消费者读取位置 */
static volatile uint32_t s_dropped;     /* 尚未随批上报的丢弃数 */
static volatile uint32_t s_drop_total;  /* 累计丢弃数（只增，用于诊断）*/
static volatile uint32_t s_read_fail;   /* 累计 I2C 读失败次数 */

static uint32_t s_batch_seq;            /* 批号：开机内递增，服务端靠它判断批间是否连续 */
static wave_batch_t s_batch;            /* 交给 main.c 的那一批（静态，避免占主任务栈）*/
static bool s_inited;

/* 保护 head/tail/dropped。采样任务和主循环可能跑在不同核上，必须加锁。
 * 用临界区（自旋锁）而不是互斥量：临界区里只有内存操作，没有任何阻塞调用，
 * 持锁时间在微秒级，用不上会引发调度的互斥量。 */
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;

/* ---------------- 生产者：采样任务 ---------------- */

static void ring_push(int16_t x, int16_t y, int16_t z, int64_t t_us)
{
    portENTER_CRITICAL(&s_lock);

    uint32_t head = s_head;
    if (head - s_tail >= RING_SAMPLES) {
        /* 满了：丢掉最老的一个。
         * ★ 一定要计数 —— 服务端要靠这个数判断"批间有没有洞"，
         *   偷偷丢掉不报，时间轴会看着是直的、实际是错的。 */
        s_tail++;
        s_dropped++;
        s_drop_total++;
    }
    s_ring[head & RING_MASK].x = x;
    s_ring[head & RING_MASK].y = y;
    s_ring[head & RING_MASK].z = z;
    s_ring[head & RING_MASK].t_us = t_us;
    s_head = head + 1;

    portEXIT_CRITICAL(&s_lock);
}

static void wave_task(void *arg)
{
    (void)arg;
    ESP_LOGI(TAG, "采样任务启动：%d Hz（周期 %d ms），攒 %d 点发一批",
             WAVE_HZ, WAVE_PERIOD_MS, WAVE_BATCH_SAMPLES);

    TickType_t last_wake = xTaskGetTickCount();
    const TickType_t period = pdMS_TO_TICKS(WAVE_PERIOD_MS);

    while (true) {
        qma7981_sample_t s;
        if (qma7981_read(&s) == ESP_OK) {
            /* 采样时刻在**读成功之后**取。取在之前的话，I2C 卡一下就会把
             * 时刻标早，批内间隔被拉歪 —— 横轴是靠这个算出来的。 */
            ring_push(s.x_raw, s.y_raw, s.z_raw, esp_timer_get_time());
        } else {
            /* 读失败就**不补点、不插值**：宁可在波形上留个洞，
             * 也不要拿一个编出来的值把洞填上。次数记下来供诊断。 */
            s_read_fail++;
        }
        vTaskDelayUntil(&last_wake, period);
    }
}

/* ---------------- 初始化 ---------------- */

esp_err_t wave_init(void)
{
#if !WAVE_ENABLE
    ESP_LOGI(TAG, "WAVE_ENABLE=0，波形采样未启用");
    return ESP_OK;
#else
    if (s_inited) {
        return ESP_OK;
    }
    s_head = s_tail = s_dropped = s_drop_total = s_read_fail = 0;
    s_batch_seq = 0;

    /*
     * 优先级 5：高于主任务（默认 1），低于 Wi-Fi / 定时器任务（22~23）。
     * 采样要准时，但不能盖过协议栈 —— 否则上传反而更慢，缓冲更容易溢出。
     * 栈 3072 字节：qma7981_read 里有 6 字节缓冲加几个浮点运算，够用。
     */
    BaseType_t ok = xTaskCreate(wave_task, "wave", 3072, NULL, 5, NULL);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "采样任务创建失败（内存不足）");
        return ESP_ERR_NO_MEM;
    }
    s_inited = true;
    return ESP_OK;
#endif
}

/* ---------------- 消费者：主循环取批 ---------------- */

const wave_batch_t *wave_take_batch(void)
{
    if (!s_inited) {
        return NULL;
    }

    portENTER_CRITICAL(&s_lock);
    if (s_head - s_tail < WAVE_BATCH_SAMPLES) {
        portEXIT_CRITICAL(&s_lock);
        return NULL;                 /* 还没攒够，这是常态，不是错误 */
    }

    wave_batch_t *b = &s_batch;
    b->n = WAVE_BATCH_SAMPLES;
    /* ★ 批号在"取走"这一刻就自增，不是"发成功"之后。
     *   这样一旦这批没送达，服务端会看到批号从 6 跳到 8 —— 它据此知道中间少了一段，
     *   不会把 7 和 9 当成连续的。反过来若改成"发成功才自增"，失败就会留下一个
     *   看不出来的洞：数据少了，时间轴却还是被当成连续的反推出来。 */
    b->batch_seq = ++s_batch_seq;
    b->dropped = s_dropped;          /* 把"从上一批到现在丢了几个"交出去 */
    s_dropped = 0;

    for (size_t i = 0; i < WAVE_BATCH_SAMPLES; i++) {
        const wave_sample_t *sp = &s_ring[(s_tail + i) & RING_MASK];
        b->xyz[i * 3 + 0] = sp->x;
        b->xyz[i * 3 + 1] = sp->y;
        b->xyz[i * 3 + 2] = sp->z;
        if (i == 0) {
            b->t_first_us = sp->t_us;
        }
        if (i == WAVE_BATCH_SAMPLES - 1) {
            b->t_last_us = sp->t_us;
        }
    }
    s_tail += WAVE_BATCH_SAMPLES;

    portEXIT_CRITICAL(&s_lock);
    return b;
}

size_t wave_pending(void)
{
    return (size_t)(s_head - s_tail);
}

uint32_t wave_read_failures(void)
{
    return s_read_fail;
}

uint32_t wave_dropped_total(void)
{
    return s_drop_total;
}
