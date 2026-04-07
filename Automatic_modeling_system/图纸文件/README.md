# 建筑自动建模系统 MVP

基于 `/workspace/jianmo/docs` 中的设计文档、模块要求和需求转化文档实现的首版系统骨架。

当前版本包含：

- `FastAPI` 后端主链路
- `React + TypeScript` 最小工作台
- 文档对应的数据结构、规则校验、建模计划、自检与导出接口
- 自动化测试

## 目录

- `jianmo/app`: 后端 API、领域模型和编排服务
- `jianmo/web`: 前端工作台
- `tests`: 后端测试
- `jianmo/generated`: 运行后生成的导出产物
- `jianmo/docs`: 原始设计文档

## 启动后端

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python -m jianmo.app.main
```

## 启动前端

```bash
cd jianmo/web
npm install
npm run dev
```

后端默认监听 `0.0.0.0:3000`，本地访问地址是 `http://127.0.0.1:3000`。
前端默认监听 `0.0.0.0:3001`，本地访问地址是 `http://127.0.0.1:3001`。
前端默认通过 Vite 代理访问 `http://127.0.0.1:3000`。
当前环境如果文件 watcher 上限较低，前端已改为 polling 模式启动，避免 `EMFILE: too many open files`。

## 测试

```bash
source .venv/bin/activate
pytest
```
