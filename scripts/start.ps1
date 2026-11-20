$ErrorActionPreference = "Stop"
Set-Location -LiteralPath (Split-Path $PSScriptRoot -Parent)
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Python virtual environment setup failed." }
}
& '.venv/Scripts/python.exe' -c "from importlib.util import find_spec; import sys; sys.exit(not all(find_spec(name) for name in ('fastapi', 'pypdfium2', 'PIL')))"
if ($LASTEXITCODE -ne 0) {
    & '.venv/Scripts/python.exe' -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Python dependency setup failed." }
}
if (-not $env:DATABASE_URL -and -not $env:PGBIN -and -not (Get-Command initdb -ErrorAction SilentlyContinue)) {
    $homePath = if ($env:DANGJIAN_PG_HOME) { $env:DANGJIAN_PG_HOME } else { Join-Path $env:LOCALAPPDATA 'DangjianQA' }
    if (-not (Test-Path -LiteralPath (Join-Path $homePath 'pgsql/bin/postgres.exe')) -and -not (Test-Path -LiteralPath '.local/postgres-binaries.zip')) {
        New-Item -ItemType Directory -Path '.local' -Force | Out-Null
        curl.exe --fail --location --output .local/postgres-binaries.zip.part https://get.enterprisedb.com/postgresql/postgresql-17.12-1-windows-x64-binaries.zip
        if ($LASTEXITCODE -ne 0) { throw "PostgreSQL download failed." }
        Move-Item -LiteralPath '.local/postgres-binaries.zip.part' -Destination '.local/postgres-binaries.zip'
    }
}
& '.venv/Scripts/python.exe' -m scripts.launch
exit $LASTEXITCODE
