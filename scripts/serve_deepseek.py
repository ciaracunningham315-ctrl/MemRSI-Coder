"""Temporary Web/API provider override. local.yaml and .env stay unchanged.

Restore the configured LAN model by stopping this server and running evocode serve.
"""
import os

import uvicorn

from evocode.api import create_app
from evocode.config import load_config


def main():
    config=load_config()
    if not os.getenv(config.models.evolution.api_key_env):
        raise SystemExit('DeepSeek API key environment variable is missing')
    config.models.local=config.models.evolution.model_copy(update={
        'model_id':'deepseek-flash','thinking':'disabled','max_output_tokens':2048,
    })
    config.raw_rag.enabled=False
    config.raw_rag.background_indexing=False
    config.raw_rag.index_when_disabled=False
    config.evolution.enabled=False
    uvicorn.run(create_app(config=config),host='127.0.0.1',port=8787)


if __name__=='__main__':
    main()
