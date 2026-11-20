import getpass
from app.vault import store_secret

if __name__ == "__main__":
    value = getpass.getpass("DeepSeek API Key (hidden): ").strip()
    if not value:
        raise SystemExit("未输入密钥。")
    store_secret(value, "deepseek")
    print("本机凭据已加密保存。")
