# Windows 远程 DWG 转换服务

当前系统已经支持把 `DWG` 上传到一台 Windows 主机上的转换服务，再回传 `DXF` 给解析器继续处理。

## Windows 侧启动

前提：

- 已安装 `ODA File Converter`
- 可通过环境变量 `JIANMO_ODAFC_PATH` 指向 `ODAFileConverter.exe`

启动命令：

```bash
python -m zdong.app.windows_dwg_converter_service
```

默认监听：

- `0.0.0.0:3010`

可选环境变量：

- `JIANMO_WINDOWS_CONVERTER_HOST`
- `JIANMO_WINDOWS_CONVERTER_PORT`
- `JIANMO_ODAFC_PATH`
- `JIANMO_DWG_CONVERTER_TOKEN`

## Linux/主系统侧配置

在运行自动建模系统的环境里设置：

```bash
export JIANMO_DWG_CONVERTER_URL="http://<windows-host>:3010/convert/dwg-to-dxf"
export JIANMO_DWG_CONVERTER_TOKEN="<shared-token>"
```

可选：

```bash
export JIANMO_DWG_CONVERTER_TIMEOUT="180"
```

## 协议说明

请求：

- `POST /convert/dwg-to-dxf`
- Body: 原始 `DWG` 二进制内容
- Header `X-Source-Filename`: 原始文件名
- Header `X-Output-Version`: 目标版本，例如 `ACAD2018`
- Header `Authorization`: `Bearer <token>`，仅当服务端配置了令牌时需要

响应：

- `200 application/octet-stream`
- Header `X-Output-Filename`: 返回的 `DXF` 文件名
- Body: 原始 `DXF` 文本/二进制内容
