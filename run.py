"""指纹浏览器工作台启动入口。

用法:
    python run.py                          # 默认 127.0.0.1:18080
    python run.py --port 8080
    python run.py --home D:\\fp-node2      # 独立数据目录（多实例/同步节点部署）
    python run.py --sync-server            # 开启同步服务器（也可在设置页开启）
"""
import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(description="指纹浏览器工作台")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    parser.add_argument("--home", help="独立数据目录（多实例部署用）")
    parser.add_argument("--sync-server", action="store_true",
                        help="启动即开启同步服务器功能")
    args = parser.parse_args()

    if args.home:
        os.environ["FPWB_HOME"] = args.home
    if args.sync_server:
        os.environ["FPWB_SYNC_SERVER"] = "1"

    # config 在导入时读取 FPWB_HOME，必须在设置环境变量之后再导入
    from app.config import DEFAULT_HOST, DEFAULT_PORT

    host = args.host or DEFAULT_HOST
    port = args.port or DEFAULT_PORT

    if os.environ.get("FPWB_SYNC_SERVER") == "1":
        from app import security

        settings = security.load_settings()
        if not settings.get("sync_server_enabled"):
            security.update_settings(sync={"sync_server_enabled": True})
        print("同步服务器模式：已开启 /api/sync/* 端点")

    uvicorn = __import__("uvicorn")
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
