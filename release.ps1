$Version = (Cat .\version.txt ) -split "\."
$Version[-1] = [int]$Version[-1] + 1
$Version = $Version -join "."


$ZipFile
Compress-Archive -Path src\* -DestinationPath $ZipFile

"$env:temp\vanchor-$version.zip"