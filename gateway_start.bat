@echo off
set PYTHON=C:\Users\User\AppData\Local\Programs\Python\Python311\python.exe
set ENI_POOL_KEY=test
echo ENI Conol Pool Gateway
echo Port: 9999 | Key: test
echo Endpoint: http://127.0.0.1:9999/v1/chat/completions
echo.
%PYTHON% gateway.py
pause
