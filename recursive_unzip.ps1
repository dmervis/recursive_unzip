# Prompt for the parent folder
$parentFolder = Read-Host "Enter the path of the parent folder"

# Check if the folder exists
if (-Not (Test-Path $parentFolder)) {
    Write-Host "The specified folder does not exist." -ForegroundColor Red
    exit
}

# Get all ZIP, TAR, and GZ files in the parent folder and its subfolders
$archiveFiles = Get-ChildItem -Path $parentFolder -Recurse -Include "*.zip", "*.tar", "*.gz"

# Iterate through each archive file and extract it
foreach ($archiveFile in $archiveFiles) {
    $destinationFolder = [System.IO.Path]::GetDirectoryName($archiveFile.FullName)

    # Create a new folder for the extracted files if it doesn't exist
    if (-Not (Test-Path $destinationFolder)) {
        New-Item -ItemType Directory -Path $destinationFolder -Force
    }

    try {
        if ($archiveFile.Extension -eq ".zip") {
            Write-Host "Extracting ZIP: $($archiveFile.FullName) to $destinationFolder"
            Expand-Archive -Path $archiveFile.FullName -DestinationPath $destinationFolder -Force
        } elseif ($archiveFile.Extension -eq ".tar") {
            Write-Host "Extracting TAR: $($archiveFile.FullName) to $destinationFolder"
            tar -xf $archiveFile.FullName -C $destinationFolder
        } elseif ($archiveFile.Extension -eq ".gz") {
            Write-Host "Extracting GZ: $($archiveFile.FullName) to $destinationFolder"
            # Extract .gz files, assuming they contain a single file
            $baseFileName = [System.IO.Path]::GetFileNameWithoutExtension($archiveFile.FullName)
            $outputFile = Join-Path -Path $destinationFolder -ChildPath $baseFileName
            
            # Use the built-in GZipStream to extract .gz files
            [System.IO.Compression.GzipStream]::new(
                [System.IO.File]::OpenRead($archiveFile.FullName),
                [System.IO.Compression.CompressionMode]::Decompress
            ).CopyTo([System.IO.File]::OpenWrite($outputFile))
        }
    } catch {
        Write-Host "Failed to extract $($archiveFile.FullName): $_" -ForegroundColor Red
    }
}

Write-Host "Extraction complete."
