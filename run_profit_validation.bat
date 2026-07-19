@echo off
echo ============================================================
echo Running Profit Optimization Validation Suite...
echo ============================================================
.venv\Scripts\python.exe run_profit_validation.py
echo ============================================================
echo Validation Complete. 
echo - Main report: analysis_results.md
echo - Timestamped report saved in validation_reports/
echo ============================================================
pause
