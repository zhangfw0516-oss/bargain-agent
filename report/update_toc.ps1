# Fill the table of contents and page numbers in the generated report (no PDF export).
$ErrorActionPreference = 'Stop'
$docx = Join-Path (Split-Path $PSScriptRoot -Parent) 'deliverables\159333_Final_Report_Bargain_Agent.docx'
$word = $null; $doc = $null
try {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    $doc = $word.Documents.Open($docx, $false, $false)
    $doc.TablesOfContents | ForEach-Object { $_.Update() }
    $doc.Fields.Update() | Out-Null
    $doc.Repaginate()
    $doc.Save()
    "pages: " + $doc.ComputeStatistics(2)
}
finally {
    if ($null -ne $doc) { $doc.Close($false) }
    if ($null -ne $word) { $word.Quit() }
}
