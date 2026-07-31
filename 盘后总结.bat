@echo off
cd /d "%~dp0"
echo ========================================
echo   盘后总结 - 收盘后复盘 + 策略生成
echo ========================================
echo.
python main.py --mode eod-summary
pause
