/*
 * help_btn.h —— 第3周：按键触发 + 本地反馈（教学求助测试消息）
 *
 * 【这个模块解决什么】
 *   第2周的数据流是「网页 → 板子」，板子只是被动执行。
 *   第3周反过来：**板子主动发起**一件事，网页上的人来回应。
 *
 *   于是出现了三种"看起来都是状态"、实际来源完全不同的东西，本课要求必须能区分：
 *
 *     ① 本地确认（板端）  —— 按键真的被按下去了，板子自己知道。**只有板子知道**，
 *                            服务端无法代替它声明这一条。
 *     ② VPS 接收（服务端）—— 消息真的到了服务器。**只有服务器知道**，
 *                            而且时间戳必须由服务器自己打（不能采信板子报的时间）。
 *     ③ 查看者回应（人）  —— 网页前的人真的点了一下。**只有人知道**。
 *
 *   三者是三个独立的事实，谁都不能替谁作证。模块里用 help_state_t 表达板端视角，
 *   服务端用 device_state / server_state / viewer_state 三列分别记录。
 *
 * 【硬件依据】乐鑫官方 ESP-BSP 的 ESP32-S3-EYE 板级定义
 *   （esp-bsp/bsp/esp32_s3_eye/include/bsp/esp32_s3_eye.h）：
 *     BSP_BUTTON_5_IO = GPIO_NUM_0   —— 板载 BOOT 键，唯一一个可自由读取的按键
 *     BSP_LED_1_IO    = GPIO_NUM_3   —— 模组电源指示灯，官方明确支持软件控制
 *   注意 GPIO3 **必须配置为开漏（OD）**，否则可能烧坏 LED（官方文档明确警告，
 *   v2.2 板为此专门加了限流电阻 R83）。
 *
 * 【为什么不用 LCD 做本地反馈】
 *   子板上那块 1.3" LCD 的引脚已经查清（SPI3：PCLK=21 / DATA0=47 / DC=43 /
 *   CS=44 / 背光=48，240x240 RGB565），ESP-IDF 也自带 esp_lcd_panel_st7789 驱动。
 *   但 RGB565 在 SPI 下的字节序需要真机确认，而本机没有板子可验证 ——
 *   **宁可不做，也不提交没验证过、看起来能跑其实不亮的代码**。
 *   详见 第三周作业文档「为什么没做 LCD」一节，接入方法已写清。
 */
#ifndef HELP_BTN_H
#define HELP_BTN_H

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

/* 板端视角的状态机。注意这只是"板子所知道的"，不代表服务端或查看者的状态。 */
typedef enum {
    HELP_IDLE = 0,      /* 空闲：当前没有进行中的求助 */
    HELP_LOCAL_ACKED,   /* ① 本地确认：按键已受理，还没发出去 */
    HELP_SENDING,       /* 正在发给 VPS */
    HELP_ACCEPTED,      /* ② VPS 已接收（收到服务端回执）—— 但还没有人回应 */
    HELP_ANSWERED,      /* ③ 查看者已回应（轮询到回应内容）*/
    HELP_CANCELLED,     /* 已取消（板端再按一次，或查看者取消）*/
    HELP_FAILED,        /* 发送失败（链路不通 / 服务端报错）*/
} help_state_t;

/* 主循环要执行的待办动作。由按键产生，取走即清。 */
typedef enum {
    HELP_ACT_NONE = 0,
    HELP_ACT_REQUEST,   /* 短按：发起一次求助 */
    HELP_ACT_CANCEL,    /* 短按（进行中）：取消当前求助 */
    HELP_ACT_RESEND,    /* 长按：强制重新发起（换一个新 event_id）*/
} help_action_t;

/* 初始化：配置按键（GPIO0 输入上拉）与 LED（GPIO3 开漏），
 * 并启动一个 20ms 的软件定时器负责去抖与 LED 闪烁节拍。
 * boot_id 用于生成可追溯的 event_id（形如 help-<mac尾号>-boot7-3）。 */
esp_err_t help_btn_init(const char *boot_id);

/* 主循环调用：取走并执行待办动作（可能发起 HTTP）。
 * 返回本次执行的动作，便于调用方打日志。 */
help_action_t help_btn_poll(void);

/* 查询/设置板端状态（状态变更会立即改变 LED 反馈）*/
help_state_t help_btn_get_state(void);
void help_btn_set_state(help_state_t st);

/* 收到查看者回应时调用：切到 HELP_ANSWERED 并给出声光提示 */
void help_btn_on_answered(void);

const char *help_state_name(help_state_t st);

/* 当前进行中的求助事件编号；空闲时为 NULL。 */
const char *help_btn_event_id(void);

#endif /* HELP_BTN_H */
