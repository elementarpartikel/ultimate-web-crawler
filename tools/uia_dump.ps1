<#
.SYNOPSIS
  Skriver ut UI Automation-trädet för ett öppet fönster (DESIGNPLAN-WCAG.md, del 2, etapp 1).

.DESCRIPTION
  Visar det som skärmläsare och Accessibility Insights får från Windows: kontrolltyp, namn,
  klass, om kontrollen kan få tangentbordsfokus, värde och vilka UIA-mönster den stöder.
  Använder kontrollvyn (ControlView), som är den vy hjälpmedel normalt läser.
  MSAA-värden (LegacyIAccessible) visas inte; använd Accessibility Insights för dem.

.PARAMETER Titel
  Del av fönstrets titel, till exempel "skärmläsarprov".

.PARAMETER Utfil
  Valfri fil som resultatet sparas i (UTF-8).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\uia_dump.ps1 -Titel "skärmläsarprov" -Utfil tools\provresultat\uia_app.txt
#>
param(
    [Parameter(Mandatory = $true)][string]$Titel,
    [string]$Utfil,
    [int]$MaxDjup = 30
)

Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$Element = [System.Windows.Automation.AutomationElement]
$Gang = [System.Windows.Automation.TreeWalker]::ControlViewWalker

$fonster = $Element::RootElement.FindAll(
    [System.Windows.Automation.TreeScope]::Children,
    [System.Windows.Automation.Condition]::TrueCondition
) | Where-Object { $_.Current.Name -like "*$Titel*" } | Select-Object -First 1
if (-not $fonster) {
    Write-Error "Hittade inget öppet fönster vars titel innehåller '$Titel'."
    exit 1
}

$rader = New-Object System.Collections.Generic.List[string]
$antal = @{ Totalt = 0; Namngivna = 0; Fokuserbara = 0 }
$typer = @{}

function Visa-Element($el, [int]$djup) {
    if ($djup -gt $MaxDjup) { return }
    $c = $el.Current
    $typ = $c.ControlType.ProgrammaticName -replace '^ControlType\.', ''
    $monster = @($el.GetSupportedPatterns() | ForEach-Object {
        $_.ProgrammaticName -replace 'PatternIdentifiers\.Pattern$', ''
    }) -join ','
    $varde = ''
    try {
        $p = $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
        $varde = $p.Current.Value
        if ($varde.Length -gt 60) { $varde = $varde.Substring(0, 60) + '…' }
    } catch { }
    $rader.Add(('{0}{1} | namn="{2}" | klass={3} | fokuserbar={4} | aktiv={5} | värde="{6}" | mönster={7}' -f
        ('  ' * $djup), $typ, $c.Name, $c.ClassName, $c.IsKeyboardFocusable, $c.IsEnabled, $varde, $monster))
    $antal.Totalt++
    if ($c.Name) { $antal.Namngivna++ }
    if ($c.IsKeyboardFocusable) { $antal.Fokuserbara++ }
    $typer[$typ] = 1 + [int]$typer[$typ]
    $barn = $Gang.GetFirstChild($el)
    while ($barn) {
        Visa-Element $barn ($djup + 1)
        $barn = $Gang.GetNextSibling($barn)
    }
}

Visa-Element $fonster 0
$typlista = ($typer.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name) $($_.Value)" }) -join ', '
$sammanfattning = @(
    "Fönster: $($fonster.Current.Name)",
    "Tid: $(Get-Date -Format 'yyyy-MM-dd HH:mm')",
    "Element: $($antal.Totalt), med namn: $($antal.Namngivna), fokuserbara: $($antal.Fokuserbara)",
    "Kontrolltyper: $typlista",
    ''
)
$resultat = $sammanfattning + $rader
$resultat | ForEach-Object { Write-Output $_ }
if ($Utfil) {
    $mapp = Split-Path -Parent $Utfil
    if ($mapp -and -not (Test-Path $mapp)) { New-Item -ItemType Directory -Path $mapp | Out-Null }
    $resultat | Out-File -FilePath $Utfil -Encoding utf8
}
