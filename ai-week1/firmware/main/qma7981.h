/*
 * qma7981.h —— ESP32-S3-EYE 板载三轴加速度计驱动接口
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 一次采样的原始值与换算值 */
typedef struct {
    int16_t x_raw;      /* 14 位二进制补码原始值 */
    int16_t y_raw;
    int16_t z_raw;
    float   ax;         /* 换算后的加速度，单位 g */
    float   ay;
    float   az;
    bool    is_new;     /* 该样本是否为传感器新产出的数据（NEWDATA 标志）*/
} qma7981_sample_t;

/*
 * 初始化 I2C 总线与传感器，并把传感器切换到 Active 模式。
 * chip_id_out 可为 NULL；非 NULL 时写回读到的芯片 ID，供上层打印自检信息。
 * 返回 ESP_OK 表示 I2C 通信成功。
 */
esp_err_t qma7981_init(uint8_t *chip_id_out);

/* 读取一次三轴加速度。返回非 ESP_OK 表示 I2C 读失败，out 内容不可信。 */
esp_err_t qma7981_read(qma7981_sample_t *out);

/* 读取指定寄存器单字节，用于调试自检。 */
esp_err_t qma7981_read_reg(uint8_t reg, uint8_t *val);

#ifdef __cplusplus
}
#endif
