param(
    [Parameter(Mandatory=$true)][string]$IssueFile,
    [Parameter(Mandatory=$true)][string]$SourceLabel,
    [Parameter(Mandatory=$true)][string]$GcpProject,
    [string]$GcpLocation = "global",
    [string]$Model = "gemini-3.1-flash-lite",
    # 싼 모델(lite)을 건너뛰고 처음부터 gemini-3.8-flash로 바로 돌린다. escalation도
    # 꺼서 3.8-flash가 불충분해도 3.8-flash로 또 재시도(중복)하지 않게 한다.
    [switch]$DirectFlash
)

if ($DirectFlash) {
    $Model = "gemini-3.8-flash"
    $env:AGENTIC_ESCALATION_MODEL = "none"
}

$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:GOOGLE_GENAI_USE_VERTEXAI = "true"
$env:GOOGLE_CLOUD_PROJECT = $GcpProject
$env:GOOGLE_CLOUD_LOCATION = $GcpLocation
$env:VERTEX_MODEL = $Model

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 로그 파일 저장은 main.py가 직접 한다(logs\<타임스탬프>.log) — PowerShell의
# Tee-Object/Start-Transcript가 이 터미널 환경에서 한글을 깨뜨리는 문제를 겪어서,
# 셸의 출력 캡처에 기대지 않기로 했다.
.\.venv\Scripts\python.exe .\main.py --analyze-code-agentic --issue-file $IssueFile --source-label $SourceLabel
