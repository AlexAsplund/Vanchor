$guid = [guid]::NewGuid().ToString() -replace(".*-","")
$TargetDir = "$env:temp\vanchor-$guid"
mkdir $TargetDir
