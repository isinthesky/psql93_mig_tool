#!/bin/bash
# DB Migration Tool을 UTF-8 locale로 실행하는 스크립트

export LC_ALL=en_US.UTF-8
export LANG=en_US.UTF-8

# 애플리케이션 실행
cd "$(dirname "$0")/db_migration_tool"
python main.py "$@"