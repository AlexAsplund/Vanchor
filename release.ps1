$Version = (Cat .\version.txt ) -split "\."
$Version[-1] = [int]$Version[-1] + 1
$Version = $Version -join "."


$ZipFile = "$env:temp\vanchor-$version.zip"

rm $ZipFile -Force -ErrorAction SilentlyContinue
cp -Force .\version.txt src\version.txt

Compress-Archive -Path src\* -DestinationPath $ZipFile

rm src\version.txt -ErrorAction SilentlyContinue

$Version | Out-File .\version.txt
