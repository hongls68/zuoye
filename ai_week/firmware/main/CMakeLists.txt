# main 组件的构建规则
# SRCS 中列出参与编译的源文件；新增 .c 文件后必须在这里登记，否则不会被编译进去
idf_component_register(
    SRCS "main.c" "qma7981.c" "camera.c" "help_btn.c"
    INCLUDE_DIRS "."
    REQUIRES
        driver            # I2C 主机驱动 (driver/i2c_master.h) + GPIO + LEDC(蜂鸣器)
        esp_wifi          # Wi-Fi STA
        esp_event         # 事件循环
        esp_netif         # 网络接口 / SNTP
        esp_http_client   # HTTP POST 上传（加速度 JSON + 摄像头 JPEG + 第3周求助消息）
        nvs_flash         # Wi-Fi 驱动依赖的非易失存储
        json              # cJSON 组装 JSON
        esp_timer         # esp_timer_get_time() + 第3周按键去抖/LED 闪烁节拍
        esp32-camera      # 新增：板载 OV2640 摄像头驱动组件（依赖见 idf_component.yml）
)
