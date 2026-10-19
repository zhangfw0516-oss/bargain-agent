$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$docxPath = Join-Path $root 'deliverables\159333_Final_Report_Bargain_Agent.docx'
$pdfPath = Join-Path $root 'deliverables\159333_Final_Report_Bargain_Agent.pdf'
$renderDir = Join-Path $PSScriptRoot 'render'
New-Item -ItemType Directory -Force $renderDir | Out-Null
Get-ChildItem -LiteralPath $renderDir -Filter 'page-*.png' -ErrorAction SilentlyContinue | Remove-Item -Force
$word = $null; $document = $null
try {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    $document = $word.Documents.Open($docxPath, $false, $false)
    $document.TablesOfContents | ForEach-Object { $_.Update() }
    $document.Fields.Update() | Out-Null
    $document.Repaginate()
    $document.Save()
    $document.ExportAsFixedFormat($pdfPath, 17)
    "pages: " + $document.ComputeStatistics(2)
    "words: " + $document.ComputeStatistics(0)
}
finally {
    if ($null -ne $document) { $document.Close($false) }
    if ($null -ne $word) { $word.Quit() }
}
# Page previews (optional): needs Poppler's pdftoppm, on PATH or in $env:PDFTOPPM
$pdftoppm = if ($env:PDFTOPPM) { $env:PDFTOPPM } else { (Get-Command pdftoppm -ErrorAction SilentlyContinue).Source }
if ($pdftoppm) {
    & $pdftoppm -png -r 60 $pdfPath (Join-Path $renderDir 'page')
    (Get-ChildItem $renderDir -Filter 'page-*.png').Count
}
