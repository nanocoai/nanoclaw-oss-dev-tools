# Read-only proof of the Windows owner's local Docker Desktop Linux engine.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {
    if ([Security.Principal.WindowsIdentity]::GetCurrent().IsSystem) {
        throw 'Run in the Windows account that owns Docker Desktop and the WSL distribution'
    }
    $Candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe'),
        (Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin\docker.exe')
    )
    $Docker = $Candidates | Where-Object { Test-Path $_ -PathType Leaf } | Select-Object -First 1
    if (-not $Docker) { throw 'Docker Desktop Windows CLI is unavailable' }
    $Endpoint = 'npipe:////./pipe/dockerDesktopLinuxEngine'
    $ErrorActionPreference = 'Continue'
    $Raw = & $Docker --host $Endpoint info --format '{{json .}}' 2>$null
    $Code = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($Code -ne 0) { throw 'Local Docker Desktop Linux engine is unavailable' }
    $Info = ($Raw | Out-String) | ConvertFrom-Json
    if ($Info.OSType -ne 'linux' -or $Info.OperatingSystem -ne 'Docker Desktop' -or -not $Info.ID) {
        throw 'Unexpected Windows Docker engine'
    }
    $OS = Get-CimInstance Win32_OperatingSystem
    @{
        status = 'passed'
        windowsVersion = $OS.Version
        windowsCaption = $OS.Caption
        dockerEndpoint = $Endpoint
        dockerEngineID = $Info.ID
        dockerServerVersion = $Info.ServerVersion
        kernelVersion = $Info.KernelVersion
        operatingSystem = $Info.OperatingSystem
        osType = $Info.OSType
    } | ConvertTo-Json -Compress
} catch {
    # No raw Docker output, environment or user configuration is exported.
    [Console]::Error.WriteLine('Windows local Docker Desktop engine check failed')
    exit 1
}
