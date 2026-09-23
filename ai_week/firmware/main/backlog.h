/*
 * backlog.h —— 断网期间的帧缓冲队列（Flash 驻留，恢复后补传）
 *
 * 对应计划书 4.2 的缓冲层。摄像头抓帧不需要网络，所以**断网时也应该继续抓**，
 * 只是把帧先放进 Flash 队列，等网络回来再按先进先出补传。
 *
 * 【为什么用"文件名排序"当队列顺序，而不是维护一个索引文件】
 *   索引文件是"两份数据要同时保持一致"—— 一旦在写索引时掉电，
 *   队列就烂了。文件名本身承载全部元数据，就没有这个不一致窗口：
 *   文件写完了，这一帧就完整；没写完（掉电），名字根本没出现。
 *   SPIFFS 本身对掉电是安全的，所以这条路不需要我们自己处理半写状态。
 *
 * 【文件名格式】BBBBBB_SSSSSSSS_TTTTTTTT.jpg
 *     BBBBBB      开机计数（NVS 里的 boot_cnt，单调递增）
 *     SSSSSSSS    本次开机内的入队序号
 *     TTTTTTTT    采集时刻的**板端 uptime（秒）**
 *   按名字做字典序排序 == 先进先出顺序。
 *
 *   ★ 为什么采集时刻要写进文件名，而不是另存一个 .txt
 *     为了不多一个"可能和主文件对不上"的副文件。补传时板端用
 *     make_timestamp_ago(now_us - TTTTTTTT*1e6) 就能还原出采集时刻串：
 *       · 已对时   → 还原成真实的 ISO 时刻
 *       · 未对时   → 还原成 uptime+<采集时的秒数>s(time_not_synced)
 *     精度是**秒级**（文件名里只存秒）。补传帧不参与任何命令闭环，
 *     所以秒级足够；这一点在文档里写明了，不当成"精确到毫秒"。
 *
 *   ★ 已知限制（宁可写出来，也不假装没有）
 *     开机计数超过 6 位（999999 次重启）或单次开机入队超过 8 位时，
 *     文件名的定长字段会溢出，字典序不再等于 FIFO。
 *     实际使用中到不了，但这是**文件名编码**这种做法的固有代价。
 */
#ifndef BACKLOG_H
#define BACKLOG_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

/* 挂载分区并扫描现有队列。可重复调用（已挂载则直接返回成功）。 */
esp_err_t backlog_init(void);

/* 分区是否可用。false 时调用方应当**如实报告本帧被丢弃**，而不是假装存下了。 */
bool backlog_ready(void);

/* 当前队列里有几帧。 */
int backlog_count(void);

/* 累计因"队列满/写不下"而丢弃的**帧数**（不包含单帧过大被拒的）。
 * 这个数会随每一帧补传给服务端，让画廊能显示"更老的帧已丢弃 N 帧"。 */
uint32_t backlog_dropped_total(void);

/* 入队一帧。
 *   boot_cnt  本次开机计数（来自 main.c 的 boot_id 生成逻辑）
 *   capture_uptime_s  采集时刻的板端 uptime（秒）
 * 队列满或分区空间不足时会**丢最老的一帧**再写，并累加 dropped 计数。
 * 返回 ESP_OK 表示确实存下了；其它值表示没存下（调用方要如实记一笔）。 */
esp_err_t backlog_put(const uint8_t *buf, size_t len,
                      uint32_t boot_cnt, int64_t capture_uptime_s);

/* 读最老一帧的元数据 + 内容。
 *   buf/cap  由调用方提供的缓冲区（建议直接用摄像头的 PSRAM 缓冲）
 *   out_len  实际读到的字节数
 *   out_capture_uptime_s  该帧采集时刻的板端 uptime（秒）
 *   out_name 该帧的文件名（补传成功后用它来删除）
 * 队列为空返回 ESP_ERR_NOT_FOUND。 */
esp_err_t backlog_peek_oldest(uint8_t *buf, size_t cap, size_t *out_len,
                              int64_t *out_capture_uptime_s,
                              char *out_name, size_t name_cap);

/* 删除最老一帧。**只有补传成功之后才该调用它** ——
 * 先删后传的话，传失败就等于凭空丢了一帧，而 dropped 计数还是 0，
 * 服务端会以为数据是完整的。 */
esp_err_t backlog_drop_oldest(void);

#endif /* BACKLOG_H */
