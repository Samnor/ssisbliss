# Create the SSIS catalog on every instance (Integration Services is a shared feature).
Add-LabAssembly 'Microsoft.SqlServer.Management.IntegrationServices'
$catalogPassword = (Get-SSMParameter -Name $Lab.catalog_password_parameter -WithDecryption $true -Region $Lab.region).Value

foreach ($inst in Get-LabSeedInstances) {
    Invoke-LabSql $inst "EXEC sp_configure 'clr enabled', 1; RECONFIGURE;" | Out-Null
    $conn = New-Object System.Data.SqlClient.SqlConnection "Data Source=localhost,$($inst.port);Initial Catalog=master;Integrated Security=SSPI;"
    $ssis = New-Object Microsoft.SqlServer.Management.IntegrationServices.IntegrationServices $conn
    if ($ssis.Catalogs['SSISDB']) {
        Write-Output "$($inst.name): SSISDB exists"
        continue
    }
    $cat = New-Object Microsoft.SqlServer.Management.IntegrationServices.Catalog ($ssis, 'SSISDB', $catalogPassword)
    $cat.Create()
    Write-Output "$($inst.name): SSISDB created"
}
