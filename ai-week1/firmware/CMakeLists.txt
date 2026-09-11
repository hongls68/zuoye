# 顶层工程文件
cmake_minimum_required(VERSION 3.16)

# 允许通过环境变量或参数指定额外组件目录（本项目暂不需要）
include($ENV{IDF_PATH}/tools/cmake/project.cmake)

project(imu_upload)
