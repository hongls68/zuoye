/*
 * camera.c —— ESP32-S3-EYE 板载 OV2640 摄像头驱动封装
 *
 * 引脚依据：ESP-IDF 官方组件 esp32-camera 中 esp32-s3-eye 板级定义
 *           (components/esp32-camera/boards/esp32s3eye.h)，与板载实物一致。
 * 关键点：SIOD/SIOC = GPIO4/GPIO5，与板载 QMA7981 加速度计共用同一组 SCCB(I2C) 总线。
 *          二者物理层都是 I2C 开漏协议，分时复用同一总线，无需额外接线。
 */
#include "camera.h"

#include "driver/i2c.h"   /* I2C_NUM_0：让摄像头 SCCB 复用加速度计所在的总线端口 */
#include "esp_log.h"
#include "app_config.h"

static const char *TAG = "camera";
static bool s_inited = false;

/* 分辨率：改这里即可切换。常用 FRAMESIZE_VGA(640x480) / SVGA(800x600) /
 *         XGA(1024x768) / UXGA(1600x1200)。宏可被 app_config.h 覆盖。
 * 注意 OV2640 + PSRAM 才支持高分辨率；分辨率越高单帧越大、上传越慢。 */
#ifndef CAMERA_FRAME_SIZE
#define CAMERA_FRAME_SIZE   FRAMESIZE_SVGA
#define CAMERA_FRAME_W      800
#define CAMERA_FRAME_H      600
#endif

/* ============ ESP32-S3-EYE 板载 OV2640 引脚（写死，无需外接）============ */
#define CAM_PIN_PWDN    -1      /* 板载未连接掉电引脚 */
#define CAM_PIN_RESET   -1      /* 板载未连接复位引脚 */
#define CAM_PIN_XCLK    15
#define CAM_PIN_SIOD    4       /* SCCB SDA —— 与 QMA7981 共用 GPIO4 */
#define CAM_PIN_SIOC    5       /* SCCB SCL —— 与 QMA7981 共用 GPIO5 */
#define CAM_PIN_D7      16
#define CAM_PIN_D6      17
#define CAM_PIN_D5      18
#define CAM_PIN_D4      12
#define CAM_PIN_D3      10
#define CAM_PIN_D2      8
#define CAM_PIN_D1      9
#define CAM_PIN_D0      11
#define CAM_PIN_VSYNC   6
#define CAM_PIN_HREF    7
#define CAM_PIN_PCLK    13

esp_err_t camera_init(void)
{
    if (s_inited) {
        return ESP_OK;
    }

    camera_config_t cfg = {
        .pin_pwdn     = CAM_PIN_PWDN,
        .pin_reset    = CAM_PIN_RESET,
        .pin_xclk     = CAM_PIN_XCLK,
        /*
         * 【关键修复】SCCB 控制总线与板载 QMA7981 加速度计共用 GPIO4/5。
         * 不能在这里写具体引脚号，否则 esp_camera_init 会走 SCCB_Init 在
         * 端口 1 上新建一条 I2C 总线、却仍用 GPIO4/5 —— 两个 I2C 控制器
         * 抢同一对物理线，导致加速度计读数失败、主循环卡死、什么都不上传。
         * 正确做法：引脚写 -1，并指定复用端口 0，让摄像头 SCCB 直接复用
         * QMA7981 已在 I2C_NUM_0 上建好的那条共享总线（I2C 是共享总线，
         * 0x12 加速度计与 0x30 摄像头共存，分时复用同一组线，无需额外接线）。
         */
        .pin_sscb_sda = -1,
        .pin_sscb_scl = -1,
        .sccb_i2c_port = I2C_NUM_0,
        .pin_d7       = CAM_PIN_D7,
        .pin_d6       = CAM_PIN_D6,
        .pin_d5       = CAM_PIN_D5,
        .pin_d4       = CAM_PIN_D4,
        .pin_d3       = CAM_PIN_D3,
        .pin_d2       = CAM_PIN_D2,
        .pin_d1       = CAM_PIN_D1,
        .pin_d0       = CAM_PIN_D0,
        .pin_vsync    = CAM_PIN_VSYNC,
        .pin_href     = CAM_PIN_HREF,
        .pin_pclk     = CAM_PIN_PCLK,

        .xclk_freq_hz = 20000000,          /* OV2640 典型主时钟 20MHz */
        .ledc_timer   = LEDC_TIMER_0,
        .ledc_channel = LEDC_CHANNEL_0,
        .pixel_format = PIXFORMAT_JPEG,    /* 直接出 JPEG，省去板端编码 */
        .frame_size   = CAMERA_FRAME_SIZE, /* 由 app_config.h 决定分辨率 */
        .jpeg_quality = CAMERA_JPEG_QUALITY,
        .fb_count     = 2,                 /* 双缓冲，抓取与上传并行更顺 */
        .grab_mode    = CAMERA_GRAB_WHEN_EMPTY,
    };

    esp_err_t err = esp_camera_init(&cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "摄像头初始化失败: %s（脱机只保留加速度上传，不影响其它功能）",
                 esp_err_to_name(err));
        return err;
    }

    s_inited = true;
    ESP_LOGI(TAG, "摄像头初始化成功（OV2640，JPEG，分辨率 %dx%d，质量 %d）",
             CAMERA_FRAME_W, CAMERA_FRAME_H, CAMERA_JPEG_QUALITY);
    return ESP_OK;
}

camera_fb_t *camera_capture(void)
{
    if (!s_inited) {
        return NULL;
    }
    /* esp_camera_fb_get 内部从 PSRAM 分配帧缓冲；返回 NULL 表示当前无可用帧 */
    camera_fb_t *fb = esp_camera_fb_get();
    if (fb == NULL) {
        ESP_LOGW(TAG, "抓取帧失败（帧缓冲暂不可用），跳过本帧");
    }
    return fb;
}
