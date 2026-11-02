# Open a COPY of a DOCX in Microsoft Word (read-only, hidden), update its fields,
# report statistics and export a PDF. The input file is never modified or saved.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File word_render.ps1 -Docx in.docx -Pdf out.pdf -Json stats.json
param(
    [Parameter(Mandatory = $true)][string]$Docx,
    [Parameter(Mandatory = $true)][string]$Pdf,
    [Parameter(Mandatory = $true)][string]$Json,
    [string]$UpdatedDocx = ''
)
$ErrorActionPreference = 'Stop'
$work = Join-Path ([System.IO.Path]::GetTempPath()) ("l2w_word_" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
$copy = Join-Path $work 'render_copy.docx'
Copy-Item -LiteralPath $Docx -Destination $copy
$word = $null
$doc = $null
$stats = [ordered]@{ input = (Resolve-Path -LiteralPath $Docx).Path; opened = $false }
try {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0          # wdAlertsNone
    $word.Options.UpdateLinksAtOpen = $false
    [string]$copyPath = $copy   # a temporary copy: the input itself is never opened
    $doc = $word.Documents.Open($copyPath)
    $stats.opened = $true
    $stats.fields_before_update = $doc.Fields.Count
    $stats.tocs = $doc.TablesOfContents.Count
    foreach ($toc in $doc.TablesOfContents) { [void]$toc.Update() }
    [void]$doc.Fields.Update()
    $doc.Repaginate()
    $stats.pages = $doc.ComputeStatistics(2)        # wdStatisticPages
    $stats.words = $doc.ComputeStatistics(0)        # wdStatisticWords
    $stats.paragraphs = $doc.Paragraphs.Count
    $stats.tables = $doc.Tables.Count
    $stats.inline_shapes = $doc.InlineShapes.Count
    $stats.shapes = $doc.Shapes.Count
    $stats.omaths = $doc.OMaths.Count
    $stats.sections = $doc.Sections.Count
    $stats.toc_entries = @($doc.TablesOfContents | ForEach-Object { $_.Range.Paragraphs.Count })
    $stats.toc_text = @($doc.TablesOfContents | ForEach-Object { $_.Range.Text })
    # visible page number of each page's footer is the PAGE field result: record per section
    $stats.section_page_formats = @($doc.Sections | ForEach-Object {
        $f = $_.Footers.Item(1)
        [ordered]@{ first_page = $_.Range.Information(3); footer_text = $f.Range.Text.Trim(); numbering_style = $f.PageNumbers.NumberStyle; restart = $f.PageNumbers.RestartNumberingAtSection; start = $f.PageNumbers.StartingNumber }
    })
    if ($UpdatedDocx -ne '') { [string]$upd = $UpdatedDocx; $doc.SaveAs2([ref]$upd) ; $stats.updated_docx = $UpdatedDocx }
    [string]$pdfPath = $Pdf
    $doc.ExportAsFixedFormat($pdfPath, 17)              # wdExportFormatPDF
    $stats.pdf = $Pdf
}
catch {
    $stats.error = $_.Exception.Message
}
finally {
    if ($doc -ne $null) { $no = 0; $doc.Close([ref]$no) }   # wdDoNotSaveChanges
    if ($word -ne $null) { $no = 0; $word.Quit([ref]$no) }
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
$stats | ConvertTo-Json -Depth 4 | Out-File -FilePath $Json -Encoding utf8
$stats | ConvertTo-Json -Depth 4
