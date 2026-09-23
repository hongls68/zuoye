/*
 * wave.h —— 第5周：三轴波形（传感器示波器）的板端采样与攒批
 *
 * 【这个模块解决什么】
 *   前四周板子每秒才读一次加速度计，一次一个点 —— 那点数据只够画"当前值"，
 *   画不出波形。要做示波器就得**连续采样**，而连续采样有两个新问题：
 *
 *   ① **节奏问题**：每秒一次的读法会跟着主循环走。主循环里还塞着 HTTP 上传、
 *      摄像头抓帧、指令轮询，随便哪一步慢一点，采样间隔就被拉歪了。
 *      所以采样必须独立成一个任务，用 vTaskDelayUntil 自己走自己的节拍。
 *
 *   ② **上传问题**：20Hz 意味着每秒 20 个点。**一个点发一次 HTTP 是不行的** ——
 *      这条链路的实测延迟在 3ms~770ms 之间剧烈抖动（见第 1 周记录），
 *      20 次/秒的建连成功率会低到没法看，而且每次都要重走 TCP 握手。
 *      所以板端**攒够一批再发**（默认 20Hz × 100 点 = 5 秒一批）。
 *      丢一批只少一段波形，不会把连接搞死 —— 批量上传对丢包是天然容错的。
 *
 * 【为什么采集和上传分成两半】
 *   采样任务只负责"把样本塞进环形缓冲"，不认识 HTTP；
 *   上传放在 main.c（那里才有 device_id / 服务器地址 / 统一的 HTTP 写法）。
 *   和第 3 周的 help_btn 是同一个套路：模块负责状态，主循环负责发请求。
 *
 * 【丢样本要如实上报，不能偷偷抹平】
 *   环形缓冲满了会覆盖最老的样本。这件事必须报给服务端（dropped 字段），
 *   因为服务端要靠它决定"能不能用采样率反推时间轴" ——
 *   批间有洞的时候累加就不成立，偷偷丢掉不报会让时间轴看起来是直的，
 *   实际却是错的。**"我丢了 3 个点"和"我一个都没丢"是两件事。**
 */
#ifndef WAVE_H
#define WAVE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

#include "app_config.h"

/* 一批的样本数（由 app_config.h 的 WAVE_BATCH_SAMPLES 决定）*/
typedef struct {
    size_t   n;                                  /* 本批实际样本数 */
    int16_t  xyz[WAVE_BATCH_SAMPLES * 3];        /* 交错存：x0,y0,z0, x1,y1,z1, … */
    int64_t  t_first_us;                         /* 批内首样本时刻（esp_timer 微秒）*/
    int64_t  t_last_us;                          /* 批内末样本时刻 */
    uint32_t batch_seq;                          /* 批号，开机内递增 */
    uint32_t dropped;                            /* 攒这批期间丢掉的样本数（如实上报）*/
} wave_batch_t;

/*
 * 启动采样任务。必须在 qma7981_init() 成功之后调用。
 * 失败返回非 ESP_OK（例如任务创建不出来）。
 */
esp_err_t wave_init(void);

/*
 * 取一批已经攒够的样本。
 * 返回 NULL 表示还没攒够（正常情况，别当错误处理）。
 * 返回的指针指向模块内部的静态缓冲，**下次调用会被覆盖** —— 用完就发，别留着。
 * 只有 main.c 一个消费者，所以没有做多消费者保护。
 */
const wave_batch_t *wave_take_batch(void);

/* 环形缓冲里当前积压了多少个样本（调试用）*/
size_t wave_pending(void);

/* 采样任务累计读失败次数（I2C 出错时不为 0，用来判断传感器是不是掉线了）*/
uint32_t wave_read_failures(void);

/* 累计丢弃样本数（含已随批上报的和尚未上报的）*/
uint32_t wave_dropped_total(void);

#endif /* WAVE_H */
