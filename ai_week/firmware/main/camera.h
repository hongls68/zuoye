/*
 * camera.h —— ESP32-S3-EYE 板载 OV2640 摄像头驱动封装
 *
 * 设计目标：
 *   1) 与原有加速度计功能完全解耦：摄像头初始化失败不影响加速度上传；
 *   2) 对外只暴露 init + capture 两个接口，主流程调用简单；
 *   3) 硬件连接全部写死在 camera.c 的引脚宏里（ESP32-S3-EYE 板载 OV2640，
 *      无需用户外接）。注意它与 QMA7981 共用 GPIO4/5 作为 SCCB（I2C）总线。
 *
 * 依赖：ESP-IDF 组件 esp32-camera（通过 `idf.py add-dependency espressif/esp32-camera` 拉取）。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "esp_camera.h"   /* 提供 camera_fb_t 等类型 */

#ifdef __cplusplus
extern "C" {
#endif

/*
 * 初始化摄像头。成功返回 ESP_OK，s_inited 置位。
 * 失败（如未找到摄像头、PSRAM 不足）返回非 ESP_OK，调用方应降级而非中止。
 */
esp_err_t camera_init(void);

/*
 * 抓取一帧 JPEG。成功返回非空 camera_fb_t*（内部缓冲区，不要 free）。
 * 调用方处理完必须调用 esp_camera_fb_return(fb) 归还，否则帧缓冲会耗尽。
 * 返回 NULL 表示抓取失败（超时/总线忙），调用方应跳过本帧。
 */
camera_fb_t *camera_capture(void);

#ifdef __cplusplus
}
#endif
