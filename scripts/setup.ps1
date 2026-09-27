$ErrorActionPreference = 'Stop'
function New-OpenGridSecret {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToHexString($bytes).ToLowerInvariant()
}

if (-not (Test-Path -LiteralPath '.env')) {
    $template = Get-Content -Raw -LiteralPath '.env.example'
    foreach ($name in @('MYSQL_PASSWORD','MYSQL_ROOT_PASSWORD','RABBITMQ_PASSWORD','GRAFANA_PASSWORD','API_KEY','READ_API_KEY','GRAFANA_DB_PASSWORD','UI_ADMIN_PASSWORD')) {
        $value = New-OpenGridSecret
        $template = [regex]::Replace($template, "(?m)^$name=.*$", "$name=$value")
    }
    Set-Content -LiteralPath '.env' -Value $template -Encoding utf8
    Write-Output 'Created .env with random local credentials. Keep this file private.'
}

# Existing local installations predate browser authentication. Add only the
# missing values; never rotate an existing operator password implicitly.
if (-not (Select-String -Quiet -Path '.env' -Pattern '^UI_ADMIN_USERNAME=')) {
    Add-Content -LiteralPath '.env' -Value 'UI_ADMIN_USERNAME=admin' -Encoding utf8
}
if (-not (Select-String -Quiet -Path '.env' -Pattern '^UI_ADMIN_PASSWORD=')) {
    Add-Content -LiteralPath '.env' -Value ("UI_ADMIN_PASSWORD={0}" -f (New-OpenGridSecret)) -Encoding utf8
}
