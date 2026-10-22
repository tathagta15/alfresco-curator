<#
    OPTIONAL. The stack does not need this script.

    `docker compose up -d` builds Share from docker/share/Dockerfile, which
    layers the same share-ui/ customisation onto the stock Share image. That
    needs no JDK, Maven, clone or Alfresco artifact credentials, and tracks the
    same 26.3 line as the repository.

    This script is the from-source alternative: it rebuilds Share from
    Alfresco's enterprise-share sources, which is worth doing only when the
    customisation has to be validated against a branch of those sources.

    It tags `alfresco-share-curator:from-source` deliberately, so it cannot
    overwrite the image compose builds. To run the stack on it:
        ALFRESCO_SHARE_IMAGE=alfresco-share-curator:from-source

    The customisation lives in this repository under share-ui/, and is injected
    into a clone of Alfresco/alfresco-enterprise-share. Injection is idempotent
    (guarded by CURATOR markers), so the script can be re-run, and the clone can
    be updated or reset without losing the change.

    Requires: gh (authenticated), git, JDK 21, Maven, Docker, and Maven
    credentials for artifacts.alfresco.com in ~/.m2/settings.xml.

    Usage:
        pwsh scripts/build_share_image.ps1
        pwsh scripts/build_share_image.ps1 -Branch release/26.2
#>
[CmdletBinding()]
param(
    [string]$ShareRepo = (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) "alfresco-enterprise-share"),
    [string]$Branch    = "release/26.2",
    [string]$Tag       = "from-source"
)

$ErrorActionPreference = "Stop"
$root    = Split-Path -Parent $PSScriptRoot
$shareUi = Join-Path $root "share-ui"
# Deliberately not alfresco/alfresco-share-base: anyone who has worked with
# Alfresco may already have that name cached, and compose would then start
# stock Share with no Curator in it and no error to say so.
$image   = "alfresco-share-curator:$Tag"

# --------------------------------------------------------------- clone / update
if (-not (Test-Path $ShareRepo)) {
    Write-Host "Cloning Alfresco/alfresco-enterprise-share into $ShareRepo"
    $parent = Split-Path -Parent $ShareRepo
    Push-Location $parent
    try {
        gh repo clone Alfresco/alfresco-enterprise-share -- --depth 1 --branch $Branch
    } finally {
        Pop-Location
    }
}

$dockerModule = Join-Path $ShareRepo "packaging/docker"
if (-not (Test-Path $dockerModule)) { throw "Not an enterprise-share checkout: $ShareRepo" }

# ------------------------------------------------------------- inject resources
$curatorRes = Join-Path $dockerModule "src/main/resources/curator"
if (Test-Path $curatorRes) { Remove-Item $curatorRes -Recurse -Force }
New-Item -ItemType Directory -Force -Path $curatorRes | Out-Null
Copy-Item (Join-Path $shareUi "web-extension/*") $curatorRes -Recurse -Force
Write-Host "Injected Curator web-extension resources"

# ---------------------------------------------------------------- inject config
$configFile = Join-Path $dockerModule "src/main/resources/web-extension-samples/share-config-custom.xml"
$config = Get-Content $configFile -Raw
$config = [regex]::Replace($config, '(?s)\s*<!-- CURATOR:BEGIN -->.*?<!-- CURATOR:END -->', '')

$block = Get-Content (Join-Path $shareUi "config/curator-share-config.xml") -Raw
$block = $block.Substring($block.IndexOf("   <!-- CURATOR:BEGIN -->"))
# CURATOR_URL is resolved at container start by substituter.sh.
$block = $block -replace '\{curatorUrl\}', 'CURATOR_URL'

$config = $config -replace '</alfresco-config>\s*$', "$block`n</alfresco-config>`n"
Set-Content -Path $configFile -Value $config -Encoding utf8 -NoNewline
Write-Host "Injected Curator Share config"

# ----------------------------------------------------------- inject substituter
$substituter = Join-Path $dockerModule "substituter.sh"
$sh = (Get-Content $substituter -Raw) -replace "`r`n", "`n"
if ($sh -notmatch "CURATOR_URL") {
    $anchor = "sed -i -e 's/REPO_HOST:REPO_PORT/'`"`$REPO_HOST:`$REPO_PORT`"'/g' /usr/local/tomcat/shared/classes/alfresco/web-extension/share-config-custom.xml"
    $addition = @"
$anchor

# Search & Diagnostic Curator: resolved at start-up so the image does not have
# to be rebuilt to relocate the service. '|' delimits because it is a URL.
if [[ `$CURATOR_URL == "" ]]; then
   CURATOR_URL=http://localhost:8600
fi

echo "Replace 'CURATOR_URL' with '`$CURATOR_URL'"

sed -i -e 's|CURATOR_URL|'"`$CURATOR_URL"'|g' /usr/local/tomcat/shared/classes/alfresco/web-extension/share-config-custom.xml
"@
    $sh = $sh.Replace($anchor, $addition)
    # LF only: this runs inside a Linux container.
    [IO.File]::WriteAllText($substituter, $sh)
    Write-Host "Injected CURATOR_URL substitution"
} else {
    Write-Host "CURATOR_URL substitution already present"
}

# ------------------------------------------------------------- inject Dockerfile
$dockerfile = Join-Path $dockerModule "Dockerfile"
$df = Get-Content $dockerfile -Raw
if ($df -notmatch "target/classes/curator") {
    $anchor = "COPY target/classes/web-extension-samples/smartfolders-amp-actions-config.xml /usr/local/tomcat/shared/classes/alfresco/web-extension"
    $df = $df.Replace($anchor, @"
$anchor

# Search & Diagnostic Curator: Surf page, template, component webscript,
# header customisation and message bundle.
COPY target/classes/curator/ /usr/local/tomcat/shared/classes/alfresco/web-extension/
"@)
    Set-Content -Path $dockerfile -Value $df -Encoding utf8 -NoNewline
    Write-Host "Injected Curator COPY into Dockerfile"
} else {
    Write-Host "Dockerfile already carries the Curator resources"
}

# ------------------------------------------------------------------------ build
$pom = Join-Path $ShareRepo "pom.xml"

Write-Host "`nBuilding the Share WAR (first run downloads a lot)..."
& mvn -f $pom -pl share -am "-DskipTests" "-Dmaven.test.skip=true" "-Dlicense.skip=true" install
if ($LASTEXITCODE -ne 0) { throw "Share WAR build failed" }

Write-Host "`nBuilding the Share Docker image..."
& mvn -f $pom -pl packaging/docker -Pbuild-docker-images "-DskipTests" "-Dmaven.test.skip=true" package
if ($LASTEXITCODE -ne 0) { throw "Share image build failed" }

# The Maven build tags with the project version; re-tag to something the
# compose file can reference without tracking SNAPSHOT numbers.
$built = (docker images "alfresco/alfresco-share-base" --format "{{.Repository}}:{{.Tag}}" |
          Select-Object -First 1)
if (-not $built) { throw "No alfresco/alfresco-share-base image was produced" }

docker tag $built $image
Write-Host "`nTagged $built as $image"
Write-Host "Run the stack on it with: ALFRESCO_SHARE_IMAGE=$image docker compose -f docker/docker-compose.yml up -d"
