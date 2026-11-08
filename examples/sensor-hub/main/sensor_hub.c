#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "board_pins.h"

static const char *TAG = "sensor_hub";

typedef struct {
    const char *name;
    float scale;
    int raw;
} channel_t;

static channel_t channels[] = {
    {"temperature", 0.01f, 2315},
    {"humidty", 0.1f, 412},
    {"pressure", 0.1f, 10132},
};

static const char *report_order[] = {"temperature", "humidity", "pressure"};

#define COUNT(a) (sizeof(a) / sizeof((a)[0]))

static channel_t *channel_by_name(const char *name)
{
    for (size_t i = 0; i < COUNT(channels); i++) {
        if (strcmp(channels[i].name, name) == 0) {
            return &channels[i];
        }
    }
    return NULL;
}

static float channel_value(const channel_t *ch)
{
    return ch->raw * ch->scale;
}

static void sample(void)
{
    for (size_t i = 0; i < COUNT(channels); i++) {
        channels[i].raw += (int)(i + 1);
    }
}

static void report(void)
{
    for (size_t i = 0; i < COUNT(report_order); i++) {
        const channel_t *ch = channel_by_name(report_order[i]);
        ESP_LOGI(TAG, "%s = %.2f", report_order[i], channel_value(ch));
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "starting, %d channels, i2c sda=%d scl=%d", (int)COUNT(channels), I2C_SDA_GPIO, I2C_SCL_GPIO);
    vTaskDelay(pdMS_TO_TICKS(3000));
    while (1) {
        sample();
        report();
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
}
