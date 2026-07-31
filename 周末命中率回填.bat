@echo off
set PYTHONIOENCODING=gbk
cd /d "%~dp0"
echo ========================================
echo   周末命中率回填 - AI方向判断准确率统计
echo ========================================
echo.
echo 说明：
echo   回填每一条决策信号之后 N 日的实际走势，
echo   输出方向命中率（看多/看空是否说中）。
echo   这是验证系统"能不能帮你赚钱"的核心指标。
echo.
echo   建议：每周末收盘后双击运行一次。
echo   数据会在 database 里自动积累，
echo   跑满 2-3 个月后命中率才有参考意义。
echo.
echo 模式：仅用本地日线缓存（--no-fetch），
echo       无需网络，约 1-2 分钟完成。
echo.
echo ========================================
echo 开始回填...
echo.
python main.py --mode outcome-backfill --no-fetch
echo.
echo ========================================
echo 回填完成。上方"方向命中率统计"即为结果。
echo 历史命中率越高（目标 55%+），系统越值得信赖。
echo 若长期低于 50%，请参考 EDGE_A_SHARE.md 考虑停用。
echo.
pause
