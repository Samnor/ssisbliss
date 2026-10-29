# Reference builds: Visual Studio-format SSIS projects (.dtproj + .sln + files), each built with
# devenv.com (what Visual Studio's Build does) and SSISBuild.exe (Microsoft's standalone build),
# so ssisbliss build can be compared with both. Results: C:\lab\work\corpus\<variant>\.
# Projects are created with the SSIS object model (ManagedDTS), the same one Visual Studio uses,
# and wrapped in a .dtproj the way Visual Studio stores them.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
foreach ($a in 'Microsoft.SqlServer.ManagedDTS') {
    $dll = Get-ChildItem 'C:\Windows\Microsoft.NET\assembly\GAC_MSIL' -Recurse -Filter "$a.dll" |
        Where-Object FullName -like '*15.0.0.0*' | Select-Object -First 1
    Add-Type -Path $dll.FullName
}
$DTS = 'Microsoft.SqlServer.Dts.Runtime'
$root = 'C:\lab\work\corpus'
Remove-Item -Recurse -Force $root -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $root | Out-Null
$vs = (Get-Content C:\lab\tools\ide-installed.txt | Where-Object { $_ -like 'vs=*' }).Substring(3)
$devenv = "$vs\Common7\IDE\devenv.com"
$ssisbuild = 'C:\lab\tools\ssisdevops\SSISBuild.exe'

function New-Package([string]$name, [int]$sqlTasks, [bool]$withParam) {
    $pkg = New-Object "$DTS.Package"
    $pkg.Name = $name
    $cm = $pkg.Connections.Add('OLEDB')
    $cm.Name = 'Target'
    $cm.ConnectionString = 'Data Source=localhost;Initial Catalog=STAGE;Provider=MSOLEDBSQL;Integrated Security=SSPI;'
    $cm.SetExpression('ConnectionString', '"Data Source=localhost;Initial Catalog=" + @[$Project::TargetDb] + ";Provider=MSOLEDBSQL;Integrated Security=SSPI;"')
    $prev = $null
    for ($i = 1; $i -le $sqlTasks; $i++) {
        $th = [Microsoft.SqlServer.Dts.Runtime.TaskHost]$pkg.Executables.Add('STOCK:SQLTask')
        $th.Name = "Step $i"
        $th.Properties['Connection'].SetValue($th, $cm.ID)
        $th.Properties['SqlStatementSource'].SetValue($th, "INSERT dbo.LoadLog (Package, RowsLoaded) VALUES (N'$name step $i', $i);")
        if ($prev) { [void]$pkg.PrecedenceConstraints.Add($prev, $th) }
        $prev = $th
    }
    if ($withParam) {
        $pp = $pkg.Parameters.Add('BatchSize', [TypeCode]::Int32)
        $pp.Value = 500
    }
    $pkg
}

function New-Variant([string]$variant, [hashtable]$opt) {
    $dir = Join-Path $root $variant
    $src = Join-Path $dir 'src'
    New-Item -ItemType Directory -Force $src | Out-Null
    $project = [Microsoft.SqlServer.Dts.Runtime.Project]::CreateProject()
    $project.Name = 'Corpus'
    $level = if ($opt.protection) { $opt.protection } else { 'DontSaveSensitive' }
    $project.ProtectionLevel = [Enum]::Parse([Microsoft.SqlServer.Dts.Runtime.DTSProtectionLevel], $level)
    foreach ($p in @(@('TargetDb', 'STAGE'), @('LoadMode', 'Delta'))) {
        $param = $project.Parameters.Add($p[0], [TypeCode]::String); $param.Value = $p[1]
    }
    if ($opt.sensitive) { $s = $project.Parameters.Add('ApiKey', [TypeCode]::String); $s.Sensitive = $true; $s.Value = 'not-a-secret' }
    $meta = $project.ConnectionManagerItems.Add('OLEDB', 'Metadata.conmgr')
    $meta.ConnectionManager.Name = 'Metadata'
    $meta.ConnectionManager.ConnectionString = 'Data Source=localhost;Initial Catalog=CONTROL;Provider=MSOLEDBSQL;Integrated Security=SSPI;'
    foreach ($spec in @(@('Load_A', 2, $false), @('Load_B', 4, [bool]$opt.pkgparam))) {
        $pkg = New-Package $spec[0] $spec[1] $spec[2]
        $pkg.ProtectionLevel = $project.ProtectionLevel
        [void]$project.PackageItems.Add($pkg, "$($spec[0]).dtsx")
        if ($opt.pkgparam -and $spec[0] -eq 'Load_A') { $project.PackageItems["$($spec[0]).dtsx"].EntryPoint = $false }
    }
    $ispac = Join-Path $dir 'created-by-object-model.ispac'
    $project.SaveTo($ispac)
    $project.Dispose()

    # Unpack into a Visual Studio project folder: files + .dtproj (manifest embedded) + .sln.
    $zip = [IO.Compression.ZipFile]::OpenRead($ispac)
    $manifest = $null
    foreach ($e in $zip.Entries) {
        $name = [Uri]::UnescapeDataString($e.FullName)
        $text = (New-Object IO.StreamReader($e.Open())).ReadToEnd()
        if ($name -eq '@Project.manifest') { $manifest = $text }
        elseif ($name -ne '[Content_Types].xml') { [IO.File]::WriteAllText((Join-Path $src $name), $text) }
    }
    $zip.Dispose()
    $manifest = $manifest -replace '^\s*<\?xml[^>]*\?>\s*', ''
    $configs = ''
    foreach ($cfg in @($opt.configs.Keys + @('Development') | Select-Object -Unique)) {
        $values = ''
        if ($opt.configs[$cfg]) {
            [xml]$params = Get-Content (Join-Path $src 'Project.params') -Raw
            foreach ($kv in $opt.configs[$cfg].GetEnumerator()) {
                $p = $params.Parameters.Parameter | Where-Object { $_.Name -eq $kv.Key }
                $id = ($p.Properties.Property | Where-Object { $_.Name -eq 'ID' }).'#text'
                $values += "            <ConfigurationSetting>`r`n              <Id>$id</Id>`r`n              <Name>Project::$($kv.Key)</Name>`r`n" +
                           "              <Value xsi:type=`"xsd:string`">$($kv.Value)</Value>`r`n            </ConfigurationSetting>`r`n"
            }
        }
        $configs += @"
    <Configuration>
      <Name>$cfg</Name>
      <Options>
        <OutputPath>bin</OutputPath>
        <ConnectionMappings />
        <ConnectionProviderMappings />
        <ConnectionSecurityMappings />
        <DatabaseStorageLocations />
        <TargetServerVersion>SQLServer2019</TargetServerVersion>
        <ParameterConfigurationValues>
$values        </ParameterConfigurationValues>
      </Options>
    </Configuration>

"@
    }
    $guid = [guid]::NewGuid().ToString()
    Set-Content -Encoding utf8 (Join-Path $src 'Corpus.dtproj') @"
<?xml version="1.0" encoding="utf-8"?>
<Project xmlns:xsi="http://www.w3.org/2000/10/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2000/10/XMLSchema">
  <DeploymentModel>Project</DeploymentModel>
  <DeploymentModelSpecificContent>
    <Manifest>
$manifest
    </Manifest>
  </DeploymentModelSpecificContent>
  <PropertyGroup Label="Globals">
    <ProjectGuid>{$guid}</ProjectGuid>
  </PropertyGroup>
  <Configurations>
$configs  </Configurations>
</Project>
"@
    $sln = [guid]::NewGuid().ToString().ToUpper()
    Set-Content -Encoding utf8 (Join-Path $src 'Corpus.sln') @"
Microsoft Visual Studio Solution File, Format Version 12.00
# Visual Studio Version 17
VisualStudioVersion = 17.0.31903.59
MinimumVisualStudioVersion = 10.0.40219.1
Project("{159641D6-6404-4A2A-AE62-294DE0FE8301}") = "Corpus", "Corpus.dtproj", "{$($guid.ToUpper())}"
EndProject
Global
	GlobalSection(SolutionConfigurationPlatforms) = preSolution
		Development|Default = Development|Default
	EndGlobalSection
	GlobalSection(ProjectConfigurationPlatforms) = postSolution
		{$($guid.ToUpper())}.Development|Default.ActiveCfg = Development
		{$($guid.ToUpper())}.Development|Default.Build.0 = Development
	EndGlobalSection
	GlobalSection(SolutionProperties) = preSolution
		HideSolutionNode = FALSE
	EndGlobalSection
EndGlobal
"@

    if ($opt.drift) {
        # Edited outside Visual Studio (a merge, a text editor): new description, package parameter added in the
        # .dtsx only, so the manifest in the .dtproj is stale.
        [xml]$x = Get-Content (Join-Path $src 'Load_B.dtsx') -Raw
        [void]$x.DocumentElement.SetAttribute('Description', 'www.microsoft.com/SqlServer/Dts', 'edited outside Visual Studio')
        $x.Save((Join-Path $src 'Load_B.dtsx'))
    }

    # Build: SSISBuild.exe and devenv.com, from separate copies so neither sees the other's output.
    foreach ($tool in 'ssisbuild', 'devenv') {
        $copy = Join-Path $dir "build-$tool"
        Copy-Item $src $copy -Recurse
        $log = Join-Path $dir "$tool.log"
        foreach ($cfg in @($opt.configs.Keys + @('Development') | Select-Object -Unique)) {
            if ($tool -eq 'ssisbuild') {
                $args = @("-p:$copy\Corpus.dtproj", "-c:$cfg", "-o:$copy\out\$cfg", '-l:INFO')
                if ($opt.strip) { $args += '-ss' }
                & $ssisbuild @args *>> $log
            } else {
                $p = Start-Process $devenv -ArgumentList @("`"$copy\Corpus.sln`"", '/Build', $cfg, '/Out', "`"$log`"") -Wait -PassThru -NoNewWindow
                "devenv exit $($p.ExitCode)" | Add-Content $log
            }
            "[$cfg] exit $LASTEXITCODE" | Add-Content $log
        }
    }
    Write-Output "$variant done"
}

New-Variant 'basic' @{ configs = @{} }
New-Variant 'configs' @{ configs = @{ Development = @{ TargetDb = 'DEV_STAGE' }; Release = @{ TargetDb = 'PROD_STAGE'; LoadMode = 'Full' } } }
New-Variant 'drift' @{ configs = @{}; drift = $true }
New-Variant 'userkey' @{ configs = @{}; protection = 'EncryptSensitiveWithUserKey'; sensitive = $true }
New-Variant 'pkgparams' @{ configs = @{}; pkgparam = $true }

$out = 'C:\lab\work\corpus.zip'
Remove-Item $out -ErrorAction SilentlyContinue
[IO.Compression.ZipFile]::CreateFromDirectory($root, $out)
Write-S3Object -BucketName $Lab.bucket -Region $Lab.region -Key debug/corpus.zip -File $out
Get-ChildItem $root -Recurse -Filter *.ispac | ForEach-Object { $_.FullName.Substring($root.Length) + "  " + $_.Length }
