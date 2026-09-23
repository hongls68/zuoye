/*
 * qma7981.c —— ESP32-S3-EYE 板载三轴加速度计 QMA7981 驱动
 *
 * 寄存器与数值依据：QST 原厂《QMA7981 Datasheet Rev. A》
 *   - 第 19 页 寄存器映射表：0x00 CHIP_ID / 0x01~0x06 数据 / 0x0F FSR / 0x10 BW / 0x11 PM
 *   - 第 20 页 RANGE<3:0> 量程与分辨率表
 *   - 第 21 页 BW<4:0> 带宽表、PM 寄存器 MODE_BIT 说明
 *   - 第 6 章：上电默认进入 standby，必须置 0x11<7>=1 才进 Active
 */
#include "qma7981.h"

#include "driver/i2c_master.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "app_config.h"

static const char *TAG = "qma7981";

/* I2C 通信超时（毫秒） */
#define I2C_TIMEOUT_MS 100

static i2c_master_bus_handle_t s_bus;
static i2c_master_dev_handle_t s_dev;
static bool s_inited = false;

/*
 * ★ I2C 总线互斥锁 —— 第5周加。
 *
 * 在此之前 qma7981_read() 只有主循环一个调用者，天然不会并发。
 * 第5周多了个 20Hz 的采样任务（wave.c），于是同一个 I2C 设备被两个任务读：
 * 一次 `i2c_master_transmit_receive` 是"写寄存器地址 + 读 6 字节"两段时序，
 * 中间被另一个任务插进来，读到的一定是错的数据 —— 而且**不会报错**，
 * 只是数值莫名其妙地跳一下。这类问题在波形上表现为"偶尔冒一个尖刺"，
 * 极易被当成"传感器噪声"放过去。
 *
 * 用普通互斥量就够：下面几个函数之间没有嵌套调用（qma7981_read 不经过
 * qma7981_read_reg），不存在自己等自己。
 *
 * 注意：摄像头的 SCCB 也走 I2C_NUM_0，但它只在 esp_camera_init() 期间写寄存器，
 * 而 init 发生在采样任务启动**之前**（见 main.c 的调用顺序），所以两者不会撞。
 */
static SemaphoreHandle_t s_i2c_mtx;

/* ---- 底层读写 ---- */

esp_err_t qma7981_read_reg(uint8_t reg, uint8_t *val)
{
    if (!s_inited || val == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_i2c_mtx != NULL) {
        xSemaphoreTake(s_i2c_mtx, portMAX_DELAY);
    }
    esp_err_t ret = i2c_master_transmit_receive(s_dev, &reg, 1, val, 1,
                                                pdMS_TO_TICKS(I2C_TIMEOUT_MS));
    if (s_i2c_mtx != NULL) {
        xSemaphoreGive(s_i2c_mtx);
    }
    return ret;
}

static esp_err_t qma7981_write_reg(uint8_t reg, uint8_t val)
{
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    uint8_t buf[2] = {reg, val};
    if (s_i2c_mtx != NULL) {
        xSemaphoreTake(s_i2c_mtx, portMAX_DELAY);
    }
    esp_err_t ret = i2c_master_transmit(s_dev, buf, sizeof(buf),
                                        pdMS_TO_TICKS(I2C_TIMEOUT_MS));
    if (s_i2c_mtx != NULL) {
        xSemaphoreGive(s_i2c_mtx);
    }
    return ret;
}

/* ---- 数据换算 ----
 *
 * 手册第 7.2 节：加速度为 14 位二进制补码，分布在两个寄存器中
 *   MSB (0x02) = ACC_X<13:6>
 *   LSB (0x01) = ACC_X<5:0> 在 bit7~bit2，bit0 是 NEWDATA 标志
 * 因此需要 (MSB << 8 | LSB & 0xFC) 再右移 2 位，得到 14 位有符号值。
 */
static inline int16_t qma7981_compose(uint8_t lsb, uint8_t msb)
{
    /* 先拼成 14 位无符号原始量（0 ~ 16383） */
    uint16_t u = (uint16_t)(((uint16_t)msb << 8) |
                            ((uint16_t)lsb & 0xFC)) >> 2;

    /*
     * 【关键】14 位二进制补码的符号扩展。
     * 0 ~ 8191 为正数；8192 ~ 16383 表示负数（-8192 ~ -1）。
     * 若不做这一步，负数会被当成大正数：
     *   实测板子平放时 x 原始量 = 15792，直接解读成 +15.4g（且已超 ±8g 量程，
     *   物理上不可能）；正确解读应为 15792 - 16384 = -592，即 -0.578g。
     * 判断 bit13（0x2000）为 1 即负数，置位高两位得到 16 位补码。
     */
    if (u & 0x2000) {
        return (int16_t)(u | 0xC000);
    }
    return (int16_t)u;
}

/* ---- 初始化 ---- */

esp_err_t qma7981_init(uint8_t *chip_id_out)
{
    esp_err_t ret;

    /* 0) 先建互斥锁 —— 下面第 3 步就要读寄存器了，锁必须已经就位 */
    if (s_i2c_mtx == NULL) {
        s_i2c_mtx = xSemaphoreCreateMutex();
        if (s_i2c_mtx == NULL) {
            ESP_LOGE(TAG, "创建 I2C 互斥锁失败（内存不足）");
            return ESP_ERR_NO_MEM;
        }
    }

    /* 1) 创建 I2C 主机总线
     *    S3-EYE 的 SDA/SCL 无外部上拉电阻，必须启用芯片内部上拉。
     */
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = QMA7981_I2C_SDA_GPIO,
        .scl_io_num = QMA7981_I2C_SCL_GPIO,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    ret = i2c_new_master_bus(&bus_cfg, &s_bus);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "创建 I2C 总线失败: %s", esp_err_to_name(ret));
        return ret;
    }

    /* 2) 挂载传感器设备 */
    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = QMA7981_I2C_ADDR,
        .scl_speed_hz = 400000,
    };
    ret = i2c_master_bus_add_device(s_bus, &dev_cfg, &s_dev);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "挂载 I2C 设备失败: %s", esp_err_to_name(ret));
        return ret;
    }
    s_inited = true;

    /* 3) 读芯片 ID 自检
     *    注意：手册第 19 页写明 0x00 的默认值是 "ANA"，即由芯片 NVM 决定，
     *    并非固定常数。0xE7 是 ESP32-S3-EYE 上的实测值，因此这里只做告警，
     *    不因为 ID 不同就直接判定失败——避免因批次差异导致板子罢工。
     */
    uint8_t chip_id = 0;
    ret = qma7981_read_reg(QMA7981_REG_CHIP_ID, &chip_id);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "读取芯片 ID 失败: %s。请检查 I2C 引脚（SDA=GPIO%d, SCL=GPIO%d）与从机地址 0x%02X",
                 esp_err_to_name(ret), QMA7981_I2C_SDA_GPIO,
                 QMA7981_I2C_SCL_GPIO, QMA7981_I2C_ADDR);
        s_inited = false;
        return ret;
    }
    if (chip_id_out != NULL) {
        *chip_id_out = chip_id;
    }
    if (chip_id != QMA7981_CHIP_ID_EXPECT) {
        ESP_LOGW(TAG, "芯片 ID = 0x%02X，与预期 0x%02X 不同。"
                      "该寄存器默认值由芯片 NVM 决定，批次不同可能不一致；"
                      "I2C 通信本身正常，继续运行。",
                 chip_id, QMA7981_CHIP_ID_EXPECT);
    } else {
        ESP_LOGI(TAG, "芯片 ID = 0x%02X，自检通过", chip_id);
    }

    /* 4) 显式设置量程，不依赖复位默认值
     *    手册第 20 页：RANGE=0100 -> ±8g -> 977 ug/LSB（即 1024 LSB/g）
     */
    ret = qma7981_write_reg(QMA7981_REG_FSR, QMA7981_RANGE_BITS);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "设置量程失败: %s", esp_err_to_name(ret));
        return ret;
    }

    /* 5) 进入 Active 模式（手册第 6 章：上电默认 standby）
     *    只置 MODE_BIT(bit7)，保留 T_RSTB_SINC_SEL 与 MCLK_SEL 的复位默认值。
     *    带宽寄存器 0x10 同样保持复位默认——默认输出速率已远高于本任务每秒 1 次的采样需求。
     */
    uint8_t pm = 0;
    ret = qma7981_read_reg(QMA7981_REG_PM, &pm);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "读取 PM 寄存器失败: %s", esp_err_to_name(ret));
        return ret;
    }
    pm |= 0x80;  /* MODE_BIT = 1 -> Active */
    ret = qma7981_write_reg(QMA7981_REG_PM, pm);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "写入 PM 寄存器失败: %s", esp_err_to_name(ret));
        return ret;
    }

    /* 6) 等待传感器就绪（手册：进入 Active 后唤醒时间约 1 ms）*/
    vTaskDelay(pdMS_TO_TICKS(20));

    ESP_LOGI(TAG, "初始化完成：量程 ±8g，灵敏度 %.0f LSB/g，Active 模式",
             QMA7981_LSB_PER_G);
    return ESP_OK;
}

/* ---- 读取一次三轴数据 ---- */

esp_err_t qma7981_read(qma7981_sample_t *out)
{
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    /* 一次性连读 0x01~0x06 共 6 字节，减少 I2C 事务次数 */
    uint8_t raw[6] = {0};
    uint8_t reg = QMA7981_REG_X_LSB;
    /* ★ 必须持锁：这 6 个字节是两次时序拼起来的，中间被别的任务插进来就会读到
     *   半新半旧的数据 —— 而且不报错，只在波形上冒个尖刺，很容易被当成噪声放过。 */
    if (s_i2c_mtx != NULL) {
        xSemaphoreTake(s_i2c_mtx, portMAX_DELAY);
    }
    esp_err_t ret = i2c_master_transmit_receive(s_dev, &reg, 1, raw, sizeof(raw),
                                                pdMS_TO_TICKS(I2C_TIMEOUT_MS));
    if (s_i2c_mtx != NULL) {
        xSemaphoreGive(s_i2c_mtx);
    }
    if (ret != ESP_OK) {
        return ret;
    }

    /* NEWDATA 标志在各轴 LSB 的 bit0；三轴均为新数据才算真正的新样本 */
    out->is_new = (raw[0] & 0x01) && (raw[2] & 0x01) && (raw[4] & 0x01);

    out->x_raw = qma7981_compose(raw[0], raw[1]);
    out->y_raw = qma7981_compose(raw[2], raw[3]);
    out->z_raw = qma7981_compose(raw[4], raw[5]);

    /*
     * 换算为 g：先按手册标称灵敏度，再套用实测标定系数。
     * x_raw 等保留的是原始 ADC 计数（未标定），便于与板端串口日志对账；
     * ax/ay/az 是标定后的物理量。
     */
    const float lsb_per_g = QMA7981_LSB_PER_G * QMA7981_CALIB_SCALE;

    out->ax = (float)out->x_raw / lsb_per_g;
    out->ay = (float)out->y_raw / lsb_per_g;
    out->az = (float)out->z_raw / lsb_per_g;

    return ESP_OK;
}
