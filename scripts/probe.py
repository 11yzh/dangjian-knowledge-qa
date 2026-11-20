import asyncio
from app.model import probe, ModelError

if __name__ == "__main__":
    try:
        asyncio.run(
            probe(
                {
                    "provider": "DEEPSEEK",
                    "model_name": "deepseek-flash",
                    "secret_ref": "deepseek",
                }
            )
        )
    except ModelError as exc:
        raise SystemExit(str(exc)) from None
    print("DEEPSEEK_JSON_OK")
