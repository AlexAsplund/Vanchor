$Version = (Cat .\version.txt ) -split "\."
$Version[-1] = [int]$Version[-1] + 1
$Version = $Version -join "."


$ZipFile = "$env:temp\vanchor-$version.zip"
rm $ZipFile -Force 
Compress-Archive -Path src\* -DestinationPath $ZipFile

mv $ZipFile C:\temp