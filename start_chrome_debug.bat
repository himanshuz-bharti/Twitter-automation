@echo off
echo ========================================================
echo Killing all running Chrome processes...
echo ========================================================
taskkill /F /IM chrome.exe /T

echo.
echo ========================================================
echo Launching Chrome with remote debugging enabled...
echo ========================================================
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222

echo.
echo Chrome has been launched! 
echo Please go to x.com and ensure you are logged in.
echo Then you can run tweet-agent!
echo.
pause
