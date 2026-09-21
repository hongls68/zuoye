/*
 * help_btn.c —— 第3周：按键触发与本地反馈闭环（板端实现）
 *
 * 一个完整的闭环在板端是这样走完的：
 *
 *   按下 BOOT 键
 *     → 本地确认（LED 慢闪，串口打印"本地已确认"）      ← 只有板子知道
 *     → 发给 VPS（LED 快闪）
 *     → 收到服务端回执（LED 常亮）                      ← 只有服务器知道
 *     → 轮询到查看者回应（LED 三连闪）                  ← 只有人知道
 *
 * 再按一次 = 取消（要区分"谁取消的"：板端取消还是查看者取消，服务端分别记录）。
 * 长按 2 秒 = 强制重发一条新的。
 *
 * 【实现要点】
 *   1) LED 闪烁与按键去抖放在 20ms 的 esp_timer 里做，
 *      主循环（约 1 秒一轮）只负责跑 HTTP —— 两者节拍差 50 倍，不能混在一起。
 *   2) 时间戳分两种来源，绝不混用：
 *        - 板端时刻（pressed_at / local_ack_at）用板子自己的钟，未对时则诚实标注；
 *        - 服务端时刻（received_at）由服务端自己打，板子不参与。
 *      这正是「本地 / VPS / 查看者三种状态可区分」在代码层面的落点。
 */
#include "help_btn.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/time.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "driver/gpio.h"
#include "driver/ledc.h"
#include "esp_log.h"
#include "esp_sntp.h"
#include "esp_timer.h"
#include "esp_http_client.h"
#include "cJSON.h"

#include "app_config.h"

static const char *TAG = "help";

#if HELP_ENABLE

/* ============ 内部状态 ============ */
static help_state_t s_state = HELP_IDLE;
static char s_event_id[64] = {0};
static char s_boot_id[32] = {0};
static uint32_t s_seq = 0;                 /* 求助事件自己的新鲜度域（不与帧的 seq 混用）*/
static volatile help_action_t s_pending = HELP_ACT_NONE;
static esp_timer_handle_t s_tick_timer = NULL;

/* 按键去抖 */
static int s_btn_stable = 1;               /* 上拉，空闲为高 */
static int s_btn_last_raw = 1;
static int s_btn_same_count = 0;
static int64_t s_press_start_us = 0;

/* LED 闪烁节拍 */
static int64_t s_led_phase_us = 0;
static int s_led_on = 0;
static int s_led_repeat_done = 0;

/* ============ LED 模式表 ============
 * 每一种板端状态对应一种**一眼能区分**的闪烁方式。
 * 之所以要做出明显差异，是因为课堂上要让学生仅凭看灯就能说出
 * "现在是本地确认了，还是服务器收到了，还是有人回应了"。
 */
typedef enum { LED_MODE_OFF, LED_MODE_ON, LED_MODE_BLINK } led_mode_t;

typedef struct {
    led_mode_t mode;
    uint16_t   on_ms;
    uint16_t   off_ms;
    uint8_t    times;      /* 0 = 一直循环；>0 = 闪这么多次后落到 then 状态 */
    help_state_t then;     /* 闪完之后的去向 */
} led_pattern_t;

static const led_pattern_t s_led_tbl[] = {
    [HELP_IDLE]        = { LED_MODE_OFF,   0,   0,   0, HELP_IDLE },
    [HELP_LOCAL_ACKED] = { LED_MODE_BLINK, 150, 850, 0, HELP_IDLE },   /* 慢闪：本地已确认 */
    [HELP_SENDING]     = { LED_MODE_BLINK, 100, 100, 0, HELP_IDLE },   /* 快闪：正在发 */
    [HELP_ACCEPTED]    = { LED_MODE_ON,    0,   0,   0, HELP_IDLE },   /* 常亮：VPS 已收到 */
    [HELP_ANSWERED]    = { LED_MODE_BLINK, 120, 120, 6,  HELP_IDLE },  /* 三连闪×2：有人回应了 */
    [HELP_CANCELLED]   = { LED_MODE_BLINK, 400, 400, 4,  HELP_IDLE },  /* 长闪：已取消 */
    [HELP_FAILED]      = { LED_MODE_BLINK, 80,  80,  10, HELP_IDLE },  /* 急闪：发送失败 */
};

static const char *s_state_names[] = {
    [HELP_IDLE]        = "IDLE",
    [HELP_LOCAL_ACKED] = "LOCAL_ACKED",
    [HELP_SENDING]     = "SENDING",
    [HELP_ACCEPTED]    = "ACCEPTED",
    [HELP_ANSWERED]    = "ANSWERED",
    [HELP_CANCELLED]   = "CANCELLED",
    [HELP_FAILED]      = "FAILED",
};

const char *help_state_name(help_state_t st)
{
    if ((int)st < 0 || st > HELP_FAILED) {
        return "?";
    }
    return s_state_names[st];
}

/* ============ 板端时间戳 ============
 * 与 main.c 的 make_device_timestamp 口径完全一致：
 * 已对时 -> ISO8601(+08:00)；未对时 -> 用运行时长并**明确标注未同步**，
 * 绝不编造一个看起来正常的日期。
 */
static void help_ts(char *buf, size_t len)
{
    if (esp_sntp_get_sync_status() == SNTP_SYNC_STATUS_COMPLETED) {
        struct timeval tv;
        gettimeofday(&tv, NULL);
        struct tm tm_info;
        localtime_r(&tv.tv_sec, &tm_info);
        char base[32];
        strftime(base, sizeof(base), "%Y-%m-%dT%H:%M:%S", &tm_info);
        snprintf(buf, len, "%s.%03ld+08:00", base, tv.tv_usec / 1000);
    } else {
        snprintf(buf, len, "uptime+%.3fs(time_not_synced)",
                 esp_timer_get_time() / 1000000.0);
    }
}

/* ============ LED ============ */
static inline void led_write(int on)
{
    /* 开漏：拉低 = 导通 = 亮；置高 = 高阻 = 灭。
     * 官方文档明确要求 GPIO3 必须开漏，否则可能烧坏 LED。 */
    gpio_set_level(HELP_LED_GPIO, on ? 0 : 1);
    s_led_on = on;
}

static void led_apply(help_state_t st, int64_t now_us)
{
    const led_pattern_t *p = &s_led_tbl[(int)st];
    int64_t dt = now_us - s_led_phase_us;

    switch (p->mode) {
    case LED_MODE_OFF:
        if (s_led_on) led_write(0);
        break;
    case LED_MODE_ON:
        if (!s_led_on) led_write(1);
        break;
    case LED_MODE_BLINK:
        if (s_led_on) {
            if (dt >= (int64_t)p->on_ms * 1000) {
                led_write(0);
                s_led_phase_us = now_us;
            }
        } else {
            if (dt >= (int64_t)p->off_ms * 1000) {
                /* 一轮亮灭结束 */
                if (p->times > 0 && ++s_led_repeat_done >= p->times) {
                    s_led_repeat_done = 0;
                    led_write(0);
                    /* 瞬态状态闪完就自动落地（回应/取消/失败都是"事件"，不是常驻状态）*/
                    help_btn_set_state(p->then);
                    return;
                }
                led_write(1);
                s_led_phase_us = now_us;
            }
        }
        break;
    }
}

/* ============ 蜂鸣器（可选，板载没有，需外接）============
 * 官方 BSP 里 BSP_CAPS_AUDIO_SPEAKER = 0 —— **ESP32-S3-EYE 板载没有扬声器/蜂鸣器**。
 * 所以这一块默认关闭，必须显式在 app_config.h 里把 BUZZER_ENABLE 打开并指定引脚。
 * 用 LEDC 输出方波：无源蜂鸣器直接可用；有源蜂鸣器也能响（相当于被方波通断）。
 */
#if BUZZER_ENABLE
static void buzzer_init(void)
{
    ledc_timer_config_t t = {
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .duty_resolution = LEDC_TIMER_10_BIT,
        .timer_num = LEDC_TIMER_1,          /* 定时器0 已被摄像头 XCLK 占用 */
        .freq_hz = 2700,
        .clk_cfg = LEDC_AUTO_CLK,
    };
    ledc_timer_config(&t);
    ledc_channel_config_t c = {
        .gpio_num = BUZZER_GPIO,
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .channel = LEDC_CHANNEL_1,
        .timer_sel = LEDC_TIMER_1,
        .duty = 0,
        .hpoint = 0,
    };
    ledc_channel_config(&c);
}

static void buzzer_beep(int ms)
{
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_1, 512);   /* 50% 占空比 */
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_1);
    vTaskDelay(pdMS_TO_TICKS(ms));
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_1, 0);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_1);
}

/* 不同的"几连音"对应不同事件 —— 与 LED 闪烁模式一一对应 */
static void buzzer_pattern(help_state_t st)
{
    int times = 0, on = 60, off = 80;
    switch (st) {
    case HELP_LOCAL_ACKED: times = 1; on = 80; break;   /* 一短声：本地确认 */
    case HELP_ACCEPTED:    times = 2; on = 60; break;   /* 两短声：VPS 已收到 */
    case HELP_ANSWERED:    times = 3; on = 60; break;   /* 三短声：有人回应 */
    case HELP_CANCELLED:   times = 1; on = 300; break;  /* 一长声：取消 */
    case HELP_FAILED:      times = 4; on = 40; off = 40; break;
    default: return;
    }
    for (int i = 0; i < times; i++) {
        buzzer_beep(on);
        if (i != times - 1) vTaskDelay(pdMS_TO_TICKS(off));
    }
}
#endif /* BUZZER_ENABLE */

/* ============ 20ms 节拍：按键去抖 + LED 闪烁 ============ */
static void tick_cb(void *arg)
{
    int64_t now_us = esp_timer_get_time();

    /* --- 按键去抖：连续 3 次（60ms）读到同一电平才认账 --- */
    int raw = gpio_get_level(HELP_BTN_GPIO);
    if (raw != s_btn_last_raw) {
        s_btn_last_raw = raw;
        s_btn_same_count = 1;
    } else if (s_btn_same_count < 100) {
        s_btn_same_count++;
    }
    if (s_btn_same_count == HELP_DEBOUNCE_SAMPLES && s_btn_stable != raw) {
        s_btn_stable = raw;
        if (raw == 0) {
            s_press_start_us = now_us;      /* 按下 */
        } else {
            int64_t dur_ms = (now_us - s_press_start_us) / 1000;
            if (dur_ms >= HELP_LONG_PRESS_MS) {
                s_pending = HELP_ACT_RESEND;
            } else if (dur_ms >= HELP_MIN_PRESS_MS) {
                s_pending = (s_state == HELP_IDLE) ? HELP_ACT_REQUEST : HELP_ACT_CANCEL;
            } else {
                ESP_LOGD(TAG, "按键按下 %lld ms，视为抖动，忽略", (long long)dur_ms);
            }
        }
    }

    /* --- LED 闪烁 --- */
    led_apply(s_state, now_us);
}

/* ============ 状态变更（统一出口，顺带打日志 + 蜂鸣）============ */
void help_btn_set_state(help_state_t st)
{
    if (st == s_state) {
        return;
    }
    ESP_LOGI(TAG, "板端状态: %s -> %s（event=%s）",
             help_state_name(s_state), help_state_name(st),
             s_event_id[0] ? s_event_id : "-");
    s_state = st;
    s_led_phase_us = esp_timer_get_time();
    s_led_repeat_done = 0;
    s_led_on = -1;          /* 强制下一次 apply 重新写一次电平 */
#if BUZZER_ENABLE
    /* 蜂鸣是阻塞的，只在状态跃迁时响一次，不影响主循环的周期上报 */
    buzzer_pattern(st);
#endif
}

help_state_t help_btn_get_state(void)
{
    return s_state;
}

const char *help_btn_event_id(void)
{
    return s_event_id[0] ? s_event_id : NULL;
}

void help_btn_on_answered(void)
{
    help_btn_set_state(HELP_ANSWERED);
}

/* ============ HTTP ============ */

/* 响应体必须读干净再关连接，否则协议栈会发 RST。
 * 详细原因见 main.c 的 http_drain_body 注释（第1周踩过的坑，这里保持一致）。 */
static void drain_body(esp_http_client_handle_t client)
{
    char buf[64];
    while (esp_http_client_read(client, buf, sizeof(buf)) > 0) {
        /* 丢弃 */
    }
}

/* 把 JSON 一次性 POST 出去，返回 HTTP 状态码（<0 表示失败）。 */
static int http_post_json(const char *url, const char *payload)
{
    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = HELP_TIMEOUT_MS,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        return -1;
    }
    esp_http_client_set_header(client, "Content-Type", "application/json");
    int status = -1;
    esp_err_t err = esp_http_client_open(client, strlen(payload));
    if (err == ESP_OK) {
        if (esp_http_client_write(client, payload, strlen(payload)) >= 0) {
            err = esp_http_client_fetch_headers(client);
        } else {
            err = ESP_FAIL;
        }
    }
    if (err == ESP_OK) {
        status = esp_http_client_get_status_code(client);
        drain_body(client);
    } else {
        ESP_LOGW(TAG, "求助请求失败: %s（链路问题，下次按键会重试）",
                 esp_err_to_name(err));
    }
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    return status;
}

/* 发起 / 取消。action = "request" | "cancel"
 *
 * device_state 是**板端自报**的状态，服务端会原样记录、不替板子编。
 * 这一点是刻意设计的：三层状态里，"本地确认"这一层只有板子有资格声明。 */
static bool help_post(const char *action, const char *event_id,
                      const char *reason, const char *device_state)
{
    char ts[48];
    help_ts(ts, sizeof(ts));

    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        return false;
    }
    cJSON_AddStringToObject(root, "device_id", DEVICE_ID);
    cJSON_AddStringToObject(root, "event_id", event_id);
    cJSON_AddStringToObject(root, "action", action);
    cJSON_AddStringToObject(root, "kind", "teach_help_test");
    cJSON_AddStringToObject(root, "device_state", device_state);
    /* 板端时刻：按下时刻与本地确认时刻都来自板子的钟 */
    cJSON_AddStringToObject(root, "pressed_at", ts);
    cJSON_AddStringToObject(root, "local_ack_at", ts);
    cJSON_AddStringToObject(root, "boot_id", s_boot_id);
    cJSON_AddNumberToObject(root, "seq", s_seq);
    if (reason != NULL) {
        cJSON_AddStringToObject(root, "reason", reason);
    }
    /* 取消要写清是谁取消的 —— 板端取消和查看者取消是两件不同的事 */
    if (strcmp(action, "cancel") == 0) {
        cJSON_AddStringToObject(root, "cancelled_by", "device");
    }

    char *payload = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (payload == NULL) {
        return false;
    }
    char url[192];
    snprintf(url, sizeof(url), "%s/api/help", SERVER_URL);
    int status = http_post_json(url, payload);
    free(payload);
    return (status >= 200 && status < 300);
}

/* 轮询查看者是否回应 / 是否被取消。返回 true 表示板端状态发生了更新。 */
static bool help_poll_viewer(void)
{
    if (s_event_id[0] == '\0') {
        return false;
    }
    char url[288];
    snprintf(url, sizeof(url), "%s/api/help/poll?device_id=%s&event_id=%s",
             SERVER_URL, DEVICE_ID, s_event_id);

    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_GET,
        .timeout_ms = HELP_TIMEOUT_MS,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        return false;
    }
    char body[768];
    int rd = 0;
    esp_err_t err = esp_http_client_open(client, 0);
    if (err == ESP_OK) {
        err = esp_http_client_fetch_headers(client);
    }
    if (err == ESP_OK && esp_http_client_get_status_code(client) == 200) {
        while (rd < (int)sizeof(body) - 1) {
            int n = esp_http_client_read(client, body + rd, sizeof(body) - 1 - rd);
            if (n <= 0) {
                break;
            }
            rd += n;
        }
    }
    body[rd] = '\0';
    esp_http_client_close(client);
    esp_http_client_cleanup(client);

    bool changed = false;
    cJSON *root = cJSON_Parse(body);
    if (root == NULL) {
        return false;
    }
    cJSON *help = cJSON_GetObjectItem(root, "help");
    if (cJSON_IsObject(help)) {
        cJSON *vs = cJSON_GetObjectItem(help, "viewer_state");
        cJSON *ss = cJSON_GetObjectItem(help, "server_state");
        cJSON *at = cJSON_GetObjectItem(help, "answer_text");
        cJSON *by = cJSON_GetObjectItem(help, "answered_by");
        if (cJSON_IsString(vs) && strcmp(vs->valuestring, "ANSWERED") == 0 &&
            s_state != HELP_ANSWERED) {
            ESP_LOGI(TAG, "★ 查看者已回应：%s（回应人：%s）",
                     cJSON_IsString(at) ? at->valuestring : "(无内容)",
                     cJSON_IsString(by) ? by->valuestring : "?");
            help_btn_on_answered();
            changed = true;
        } else if (cJSON_IsString(ss) && strcmp(ss->valuestring, "CANCELLED") == 0 &&
                   s_state != HELP_CANCELLED && s_state != HELP_ANSWERED) {
            ESP_LOGW(TAG, "★ 该求助已被取消（查看者侧操作）");
            help_btn_set_state(HELP_CANCELLED);
            changed = true;
        }
    }
    cJSON_Delete(root);
    return changed;
}

/* ============ 待办动作的执行 ============ */

static void do_request(bool resend)
{
    if (resend && s_event_id[0] != '\0') {
        ESP_LOGW(TAG, "长按：取消旧的求助 %s，重新发起一条", s_event_id);
        help_post("cancel", s_event_id, "superseded_by_resend", "CANCELLED");
    }
    /* 求助事件编号由**板子自己生成** —— 因为这一次是设备主动发起，
     * 与第2周"网页发起、服务端生成 request_id"方向正好相反。 */
    s_seq++;
    snprintf(s_event_id, sizeof(s_event_id), "help-%s-%lu",
             s_boot_id, (unsigned long)s_seq);

    /* ① 本地确认：先让用户看到"按下去了"，再去碰网络。
     *    顺序很重要 —— 本地反馈不能等网络，否则断网时按下去毫无反应。 */
    ESP_LOGI(TAG, "① 本地已确认：教学求助测试消息（event=%s）", s_event_id);
    help_btn_set_state(HELP_LOCAL_ACKED);

    ESP_LOGI(TAG, "② 正在发送到 VPS…");
    help_btn_set_state(HELP_SENDING);
    if (help_post("request", s_event_id, NULL, "LOCAL_ACKED")) {
        /* ② 服务端回执 2xx 才说明"VPS 已接收"，这一步是服务端给的证据 */
        ESP_LOGI(TAG, "② VPS 已接收（LED 转常亮），等待查看者回应");
        help_btn_set_state(HELP_ACCEPTED);
    } else {
        ESP_LOGE(TAG, "发送失败：VPS 未确认收到（LED 急闪）");
        help_btn_set_state(HELP_FAILED);
    }
}

static void do_cancel(void)
{
    ESP_LOGW(TAG, "取消求助 %s（由板端按键发起）", s_event_id);
    if (help_post("cancel", s_event_id, "user_cancelled_on_device", "CANCELLED")) {
        help_btn_set_state(HELP_CANCELLED);
    } else {
        ESP_LOGE(TAG, "取消请求没发出去，服务端仍认为该求助进行中");
        help_btn_set_state(HELP_FAILED);
    }
}

help_action_t help_btn_poll(void)
{
    help_action_t act = s_pending;
    if (act == HELP_ACT_NONE) {
        /* 没有按键事件时，顺便问一下查看者有没有回应 */
        if (s_state == HELP_ACCEPTED || s_state == HELP_LOCAL_ACKED ||
            s_state == HELP_SENDING) {
            help_poll_viewer();
        }
        return HELP_ACT_NONE;
    }
    s_pending = HELP_ACT_NONE;

    switch (act) {
    case HELP_ACT_REQUEST: do_request(false); break;
    case HELP_ACT_RESEND:  do_request(true);  break;
    case HELP_ACT_CANCEL:  do_cancel();       break;
    default: break;
    }
    return act;
}

/* ============ 初始化 ============ */
esp_err_t help_btn_init(const char *boot_id)
{
    strncpy(s_boot_id, boot_id ? boot_id : "unknown", sizeof(s_boot_id) - 1);
    s_boot_id[sizeof(s_boot_id) - 1] = '\0';

    /* 按键：GPIO0，内部上拉，空闲高、按下低 */
    gpio_config_t btn = {
        .pin_bit_mask = 1ULL << HELP_BTN_GPIO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    esp_err_t err = gpio_config(&btn);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "按键 GPIO%d 配置失败: %s", HELP_BTN_GPIO,
                 esp_err_to_name(err));
        return err;
    }

    /* LED：GPIO3，**开漏**（官方要求，否则可能烧 LED）*/
    gpio_config_t led = {
        .pin_bit_mask = 1ULL << HELP_LED_GPIO,
        .mode = GPIO_MODE_OUTPUT_OD,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    err = gpio_config(&led);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "LED GPIO%d 配置失败: %s", HELP_LED_GPIO,
                 esp_err_to_name(err));
        return err;
    }
    gpio_set_level(HELP_LED_GPIO, 1);      /* 高阻 = 灭 */
    s_led_on = 0;

#if BUZZER_ENABLE
    buzzer_init();
    ESP_LOGI(TAG, "蜂鸣器已启用（GPIO%d，外接）", BUZZER_GPIO);
#else
    ESP_LOGI(TAG, "蜂鸣器未启用（板载无蜂鸣器，需外接后把 BUZZER_ENABLE 置 1）");
#endif

    s_btn_stable = gpio_get_level(HELP_BTN_GPIO);
    s_btn_last_raw = s_btn_stable;

    const esp_timer_create_args_t targs = {
        .callback = tick_cb,
        .arg = NULL,
        .name = "help_tick",
    };
    err = esp_timer_create(&targs, &s_tick_timer);
    if (err != ESP_OK) {
        return err;
    }
    err = esp_timer_start_periodic(s_tick_timer, 20 * 1000);   /* 20ms */
    if (err != ESP_OK) {
        return err;
    }

    ESP_LOGI(TAG, "求助按键就绪：按键 GPIO%d（短按=发起/取消，长按 %dms=重发）",
             HELP_BTN_GPIO, HELP_LONG_PRESS_MS);
    ESP_LOGI(TAG, "本地反馈：LED GPIO%d 开漏（慢闪=本地已确认 / 快闪=发送中 / "
                  "常亮=VPS已接收 / 三连闪=查看者已回应 / 急闪=失败）",
             HELP_LED_GPIO);
    return ESP_OK;
}

#else  /* HELP_ENABLE == 0 */

/* 关掉时保留空实现，让 main.c 不必到处加 #if */
esp_err_t help_btn_init(const char *boot_id) { (void)boot_id; return ESP_OK; }
help_action_t help_btn_poll(void) { return HELP_ACT_NONE; }
help_state_t help_btn_get_state(void) { return HELP_IDLE; }
void help_btn_set_state(help_state_t st) { (void)st; }
void help_btn_on_answered(void) { }
const char *help_btn_event_id(void) { return NULL; }
const char *help_state_name(help_state_t st) { (void)st; return "DISABLED"; }

#endif /* HELP_ENABLE */
