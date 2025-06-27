@echo off
echo Launching VigilSiddhi Python agents in parallel...
echo Press Ctrl+C to stop manually.
echo.


:: main.py
::start "" python "C:\Users\DELL\Desktop\VigilSiddhi_V1\frontend\main.py"


:: KMX Agents
start "" python "C:\Users\DELL\Desktop\VigilSiddhi_V1\backend\KMX_Agent\kmx_config_agent.py"
start "" python "C:\Users\DELL\Desktop\VigilSiddhi_V1\backend\KMX_Agent\kmx_config_consumer.py"



:: D9800 Agents
start "" python "C:\Users\DELL\Desktop\VigilSiddhi_V1\backend\D9800_Agent\ird_config_agent.py"
start "" python "C:\Users\DELL\Desktop\VigilSiddhi_V1\backend\D9800_Agent\ird_config_consumer.py"
start "" python "C:\Users\DELL\Desktop\VigilSiddhi_V1\backend\D9800_Agent\ird_trend_agent.py"
start "" python "C:\Users\DELL\Desktop\VigilSiddhi_V1\backend\D9800_Agent\ird_trend_consumer.py"

:: iLO Agents
start "" python "C:\Users\DELL\Desktop\VigilSiddhi_V1\backend\iLO_Agent\ilo_health_agent.py"
start "" python "C:\Users\DELL\Desktop\VigilSiddhi_V1\backend\iLO_Agent\ilo_health_consumer.py"

:: PGM Routing Agents
start "" python "C:\Users\DELL\Desktop\VigilSiddhi_V1\backend\PGM_ROUTING_Agent\pgm_routing_config_agent.py"
start "" python "C:\Users\DELL\Desktop\VigilSiddhi_V1\backend\PGM_ROUTING_Agent\pgm_routing_config_consumer.py"


:: websokcet
::start "" python "C:\Users\DELL\Desktop\VigilSiddhi_V1\backend\websocket_notifier.py"





echo.
echo All agents started.
pause






echo.
echo All scripts completed.
pause
