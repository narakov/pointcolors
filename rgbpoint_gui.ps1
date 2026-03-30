param(
    [switch]$SmokeTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName PresentationFramework
Add-Type -AssemblyName PresentationCore
Add-Type -AssemblyName WindowsBase
Add-Type -AssemblyName System.Windows.Forms

$script:ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:StateFile = Join-Path $script:ProjectRoot "rgbpoint_gui_state.json"
$script:CrashLogFile = Join-Path $script:ProjectRoot "rgbpoint_gui_crash.log"
$script:RunTraceFile = Join-Path $script:ProjectRoot "rgbpoint_gui_run.log"
$script:CurrentProcess = $null
$script:IsRunning = $false
$script:StatusTimer = $null
$script:LastProcessError = ""
$script:BackendStdoutFile = $null
$script:BackendStderrFile = $null
$script:BackendStdoutCharsRead = 0
$script:BackendStderrCharsRead = 0

$script:Profiles = [ordered]@{
    standard = @{
        Key = "standard"
        Title = "Standard"
        Description = "One pass. Baseline TerraPhoto-style projection with full coverage."
    }
    roll_minus = @{
        Key = "roll_minus"
        Title = "Roll Minus"
        Description = "One pass. Often improves color alignment where the roll sign is inverted."
    }
    enhanced = @{
        Key = "enhanced"
        Title = "Enhanced"
        Description = "Two passes. Uses Roll Minus as primary and fills black gaps from Standard."
    }
}

function Write-ErrorLog {
    param([string]$Message)
    try {
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff"
        Add-Content -Path $script:CrashLogFile -Value ("[$stamp] " + $Message) -Encoding UTF8
    }
    catch {
    }
}

function Write-RunTrace {
    param([string]$Message)
    try {
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff"
        Add-Content -Path $script:RunTraceFile -Value ("[$stamp] " + $Message) -Encoding UTF8
    }
    catch {
    }
}

function ConvertTo-QuotedArgument {
    param([string]$Value)

    if ($null -eq $Value) { return '""' }
    if ($Value -eq "") { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }

    $escaped = $Value -replace '(\\*)"', '$1$1\"'
    $escaped = $escaped -replace '(\\+)$', '$1$1'
    return '"' + $escaped + '"'
}

function Get-ProfileDescription {
    param([string]$Key)
    if ($script:Profiles.Contains($Key)) {
        return $script:Profiles[$Key].Description
    }
    return $script:Profiles["enhanced"].Description
}

function Get-SavedState {
    if (-not (Test-Path $script:StateFile)) { return @{} }
    try {
        return Get-Content -Raw -Path $script:StateFile | ConvertFrom-Json -AsHashtable
    }
    catch {
        return @{}
    }
}

function Get-StateValue {
    param(
        [hashtable]$State,
        [string]$Key,
        [string]$DefaultValue
    )
    if ($null -ne $State -and $State.ContainsKey($Key) -and $null -ne $State[$Key]) {
        return [string]$State[$Key]
    }
    return $DefaultValue
}

function Set-SavedState {
    param($Controls)
    $data = [ordered]@{
        las = $Controls.LasTextBox.Text
        cal = $Controls.CalTextBox.Text
        iml = $Controls.ImlTextBox.Text
        images_dir = $Controls.ImagesTextBox.Text
        output = $Controls.OutputTextBox.Text
        profile = $Controls.ProfileComboBox.SelectedValue
        chunk_size = $Controls.ChunkSizeTextBox.Text
        default_color = $Controls.DefaultColorTextBox.Text
    }
    try {
        ($data | ConvertTo-Json -Depth 3) | Set-Content -Path $script:StateFile -Encoding UTF8
    }
    catch {
    }
}

function Add-LogLine {
    param($Controls, [string]$Message)
    if ($null -eq $Message) { $Message = "" }
    $Controls.LogTextBox.AppendText($Message + [Environment]::NewLine)
    $Controls.LogTextBox.ScrollToEnd()
    Write-RunTrace $Message
}

function Flush-BackendLogs {
    param($Controls)

    if ($script:BackendStdoutFile -and (Test-Path $script:BackendStdoutFile)) {
        $stdoutText = Get-Content -Raw -Path $script:BackendStdoutFile -ErrorAction SilentlyContinue
        if ($null -ne $stdoutText) {
            if ($script:BackendStdoutCharsRead -gt $stdoutText.Length) {
                $script:BackendStdoutCharsRead = 0
            }
            if ($stdoutText.Length -gt $script:BackendStdoutCharsRead) {
                $delta = $stdoutText.Substring($script:BackendStdoutCharsRead)
                $script:BackendStdoutCharsRead = $stdoutText.Length
                foreach ($line in ($delta -split "\r\n|\n|\r")) {
                    if (-not [string]::IsNullOrWhiteSpace($line)) {
                        Add-LogLine $Controls $line
                    }
                }
            }
        }
    }

    if ($script:BackendStderrFile -and (Test-Path $script:BackendStderrFile)) {
        $stderrText = Get-Content -Raw -Path $script:BackendStderrFile -ErrorAction SilentlyContinue
        if ($null -ne $stderrText) {
            if ($script:BackendStderrCharsRead -gt $stderrText.Length) {
                $script:BackendStderrCharsRead = 0
            }
            if ($stderrText.Length -gt $script:BackendStderrCharsRead) {
                $delta = $stderrText.Substring($script:BackendStderrCharsRead)
                $script:BackendStderrCharsRead = $stderrText.Length
                foreach ($line in ($delta -split "\r\n|\n|\r")) {
                    if (-not [string]::IsNullOrWhiteSpace($line)) {
                        $script:LastProcessError = $line
                        Add-LogLine $Controls ("ERROR: " + $line)
                    }
                }
            }
        }
    }
}

function Get-LogTail {
    param(
        $Controls,
        [int]$MaxLines = 18
    )

    $text = $Controls.LogTextBox.Text
    if ([string]::IsNullOrWhiteSpace($text)) { return "" }

    $lines = @($text -split "\r\n|\n|\r" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($lines.Count -le $MaxLines) {
        return ($lines -join [Environment]::NewLine)
    }

    return ($lines[($lines.Count - $MaxLines)..($lines.Count - 1)] -join [Environment]::NewLine)
}

function Set-Running {
    param($Controls, [bool]$Running)
    $script:IsRunning = $Running
    $state = -not $Running

    $controlsToToggle = @(
        $Controls.LasTextBox,
        $Controls.CalTextBox,
        $Controls.ImlTextBox,
        $Controls.ImagesTextBox,
        $Controls.OutputTextBox,
        $Controls.ChunkSizeTextBox,
        $Controls.DefaultColorTextBox,
        $Controls.ProfileComboBox,
        $Controls.LasBrowseButton,
        $Controls.CalBrowseButton,
        $Controls.ImlBrowseButton,
        $Controls.ImagesBrowseButton,
        $Controls.OutputBrowseButton,
        $Controls.ClearButton
    )

    foreach ($control in $controlsToToggle) {
        $control.IsEnabled = $state
    }

    $Controls.RunButton.IsEnabled = -not $Running
    $Controls.RunButton.Content = if ($Running) { "Processing..." } else { "Start Colorization" }
    $Controls.ProgressBar.IsIndeterminate = $Running
    if (-not $Running) {
        Flush-BackendLogs $Controls
    }
}

function Get-SuggestedOutputPath {
    param([string]$LasPath)
    if ([string]::IsNullOrWhiteSpace($LasPath)) { return "" }
    try {
        $path = [System.IO.Path]::GetFullPath($LasPath)
        $directory = [System.IO.Path]::GetDirectoryName($path)
        $stem = [System.IO.Path]::GetFileNameWithoutExtension($path)
        return [System.IO.Path]::Combine($directory, "${stem}_gui_rgb.LAS")
    }
    catch {
        return ""
    }
}

function Show-OpenDialog {
    param(
        [string]$Title,
        [string]$Filter,
        [string]$InitialPath
    )
    $dialog = New-Object Microsoft.Win32.OpenFileDialog
    $dialog.Title = $Title
    $dialog.Filter = $Filter
    if ($InitialPath -and (Test-Path $InitialPath)) {
        $initialItem = Get-Item $InitialPath
        $dialog.InitialDirectory = if ($initialItem.PSIsContainer) { $initialItem.FullName } else { $initialItem.DirectoryName }
    }
    if ($dialog.ShowDialog()) {
        return $dialog.FileName
    }
    return $null
}

function Show-SaveDialog {
    param(
        [string]$Title,
        [string]$InitialPath
    )
    $dialog = New-Object Microsoft.Win32.SaveFileDialog
    $dialog.Title = $Title
    $dialog.Filter = "LAS Files (*.las)|*.las|All Files (*.*)|*.*"
    $dialog.DefaultExt = ".las"
    if ($InitialPath) {
        try {
            $dialog.InitialDirectory = [System.IO.Path]::GetDirectoryName($InitialPath)
            $dialog.FileName = [System.IO.Path]::GetFileName($InitialPath)
        }
        catch {
        }
    }
    if ($dialog.ShowDialog()) {
        return $dialog.FileName
    }
    return $null
}

function Show-FolderDialog {
    param([string]$Description, [string]$InitialPath)
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = $Description
    $dialog.ShowNewFolderButton = $false
    if ($InitialPath -and (Test-Path $InitialPath)) {
        $dialog.SelectedPath = $InitialPath
    }
    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        return $dialog.SelectedPath
    }
    return $null
}

function ConvertTo-DefaultColor {
    param([string]$Value)

    if ($null -eq $Value) {
        return "0,0,0"
    }

    $parts = @()
    foreach ($match in [regex]::Matches($Value, '-?\d+')) {
        $parts += [int]$match.Value
    }

    if ($parts.Count -eq 0) {
        return "0,0,0"
    }
    if ($parts.Count -eq 1) {
        $parts = @($parts[0], $parts[0], $parts[0])
    }
    elseif ($parts.Count -eq 2) {
        $parts = @($parts[0], $parts[1], 0)
    }
    elseif ($parts.Count -ne 3) {
        throw "bad"
    }

    $outOfRangeParts = @($parts | Where-Object { $_ -lt 0 -or $_ -gt 255 })
    if ($outOfRangeParts.Count -gt 0) {
        throw "bad"
    }

    return ($parts -join ",")
}

function Test-Inputs {
    param($Controls)

    $required = [ordered]@{
        "Point cloud" = $Controls.LasTextBox.Text.Trim()
        "Calibration" = $Controls.CalTextBox.Text.Trim()
        "Image list" = $Controls.ImlTextBox.Text.Trim()
        "Images folder" = $Controls.ImagesTextBox.Text.Trim()
        "Output LAS" = $Controls.OutputTextBox.Text.Trim()
    }

    $missing = @($required.GetEnumerator() | Where-Object { [string]::IsNullOrWhiteSpace($_.Value) } | ForEach-Object { $_.Key })
    if ($missing.Count -gt 0) {
        [System.Windows.MessageBox]::Show(
            "Please fill these fields:`n`n$($missing -join "`n")",
            $APP_TITLE,
            [System.Windows.MessageBoxButton]::OK,
            [System.Windows.MessageBoxImage]::Warning
        ) | Out-Null
        return $null
    }

    foreach ($entry in $required.GetEnumerator()) {
        if ($entry.Key -eq "Output LAS") { continue }
        if (-not (Test-Path $entry.Value)) {
            [System.Windows.MessageBox]::Show(
                "$($entry.Key) not found:`n$($entry.Value)",
                $APP_TITLE,
                [System.Windows.MessageBoxButton]::OK,
                [System.Windows.MessageBoxImage]::Error
            ) | Out-Null
            return $null
        }
    }

    try {
        $chunkSize = [int]$Controls.ChunkSizeTextBox.Text.Trim()
        if ($chunkSize -le 0) { throw "bad" }
    }
    catch {
        [System.Windows.MessageBox]::Show(
            "Chunk size must be a positive integer.",
            $APP_TITLE,
            [System.Windows.MessageBoxButton]::OK,
            [System.Windows.MessageBoxImage]::Error
        ) | Out-Null
        return $null
    }

    try {
        $defaultColor = ConvertTo-DefaultColor $Controls.DefaultColorTextBox.Text
        $Controls.DefaultColorTextBox.Text = $defaultColor
    }
    catch {
        [System.Windows.MessageBox]::Show(
            "Default color must look like 0,0,0. The app also accepts 0,0 or 0 and will expand it automatically.",
            $APP_TITLE,
            [System.Windows.MessageBoxButton]::OK,
            [System.Windows.MessageBoxImage]::Error
        ) | Out-Null
        return $null
    }

    try {
        $lasPath = [System.IO.Path]::GetFullPath($required["Point cloud"])
        $outputPath = [System.IO.Path]::GetFullPath($required["Output LAS"])
        if ($lasPath -eq $outputPath) {
            throw "same"
        }
    }
    catch {
        [System.Windows.MessageBox]::Show(
            "Output LAS must be different from the source LAS.",
            $APP_TITLE,
            [System.Windows.MessageBoxButton]::OK,
            [System.Windows.MessageBoxImage]::Error
        ) | Out-Null
        return $null
    }

    return [ordered]@{
        las = $lasPath
        cal = [System.IO.Path]::GetFullPath($required["Calibration"])
        iml = [System.IO.Path]::GetFullPath($required["Image list"])
        images_dir = [System.IO.Path]::GetFullPath($required["Images folder"])
        output = $outputPath
        profile = [string]$Controls.ProfileComboBox.SelectedValue
        chunk_size = $chunkSize
        default_color = $defaultColor
    }
}

$xaml = @"
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="RGB Point Cloud Colorizer"
        Height="980"
        Width="1280"
        MinHeight="820"
        MinWidth="1080"
        Background="#EFE7D8"
        WindowStartupLocation="CenterScreen">
    <Window.Resources>
        <Style TargetType="Border" x:Key="CardBorder">
            <Setter Property="Background" Value="#FAF5EB"/>
            <Setter Property="CornerRadius" Value="18"/>
            <Setter Property="Padding" Value="18"/>
            <Setter Property="Margin" Value="0,0,0,12"/>
        </Style>
        <Style TargetType="TextBlock" x:Key="CardTitle">
            <Setter Property="FontFamily" Value="Bahnschrift"/>
            <Setter Property="FontSize" Value="16"/>
            <Setter Property="FontWeight" Value="Bold"/>
            <Setter Property="Foreground" Value="#1F3A35"/>
        </Style>
        <Style TargetType="TextBlock" x:Key="BodyText">
            <Setter Property="FontFamily" Value="Bahnschrift"/>
            <Setter Property="FontSize" Value="12"/>
            <Setter Property="Foreground" Value="#3B3934"/>
            <Setter Property="TextWrapping" Value="Wrap"/>
        </Style>
        <Style TargetType="TextBlock" x:Key="MutedText">
            <Setter Property="FontFamily" Value="Bahnschrift"/>
            <Setter Property="FontSize" Value="11"/>
            <Setter Property="Foreground" Value="#6C675E"/>
            <Setter Property="TextWrapping" Value="Wrap"/>
        </Style>
        <Style TargetType="TextBox">
            <Setter Property="FontFamily" Value="Bahnschrift"/>
            <Setter Property="FontSize" Value="12"/>
            <Setter Property="Padding" Value="10,7"/>
            <Setter Property="Background" Value="#FFFDF8"/>
            <Setter Property="BorderBrush" Value="#C8BCA8"/>
            <Setter Property="BorderThickness" Value="1"/>
        </Style>
        <Style TargetType="ComboBox">
            <Setter Property="FontFamily" Value="Bahnschrift"/>
            <Setter Property="FontSize" Value="12"/>
            <Setter Property="Padding" Value="8,6"/>
            <Setter Property="Background" Value="#FFFDF8"/>
            <Setter Property="BorderBrush" Value="#C8BCA8"/>
            <Setter Property="BorderThickness" Value="1"/>
        </Style>
        <Style TargetType="Button" x:Key="PrimaryButton">
            <Setter Property="FontFamily" Value="Bahnschrift"/>
            <Setter Property="FontSize" Value="12"/>
            <Setter Property="FontWeight" Value="Bold"/>
            <Setter Property="Foreground" Value="White"/>
            <Setter Property="Background" Value="#D97F3F"/>
            <Setter Property="BorderBrush" Value="#D97F3F"/>
            <Setter Property="BorderThickness" Value="0"/>
            <Setter Property="Padding" Value="14,9"/>
            <Setter Property="Cursor" Value="Hand"/>
        </Style>
        <Style TargetType="Button" x:Key="GhostButton">
            <Setter Property="FontFamily" Value="Bahnschrift"/>
            <Setter Property="FontSize" Value="12"/>
            <Setter Property="Foreground" Value="#2F2D28"/>
            <Setter Property="Background" Value="#D7D1C3"/>
            <Setter Property="BorderBrush" Value="#D7D1C3"/>
            <Setter Property="BorderThickness" Value="0"/>
            <Setter Property="Padding" Value="12,9"/>
            <Setter Property="Cursor" Value="Hand"/>
        </Style>
    </Window.Resources>
    <Grid Margin="22">
        <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="8"/>
            <RowDefinition Height="240"/>
        </Grid.RowDefinitions>
        <Grid.ColumnDefinitions>
            <ColumnDefinition Width="3*"/>
            <ColumnDefinition Width="2*"/>
        </Grid.ColumnDefinitions>

        <StackPanel Grid.Row="0" Grid.Column="0" Grid.ColumnSpan="2" Margin="0,0,0,14">
            <TextBlock Text="RGB Point Cloud Colorizer"
                       FontFamily="Georgia"
                       FontSize="28"
                       FontWeight="Bold"
                       Foreground="#1F3A35"/>
            <TextBlock Text="Load LAS, camera calibration, IML, and the full image folder. Run coloring from a desktop interface."
                       FontFamily="Bahnschrift"
                       FontSize="13"
                       Foreground="#5A564F"
                       Margin="0,6,0,0"/>
        </StackPanel>

        <Grid Grid.Row="1" Grid.Column="0" Margin="0,0,12,0">
            <Grid.RowDefinitions>
                <RowDefinition Height="Auto"/>
                <RowDefinition Height="Auto"/>
            </Grid.RowDefinitions>

            <Border Grid.Row="0" Style="{StaticResource CardBorder}">
                <Grid>
                    <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="Auto"/>
                    </Grid.ColumnDefinitions>
                    <Grid.RowDefinitions>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="Auto"/>
                    </Grid.RowDefinitions>

                    <TextBlock Grid.Row="0" Grid.ColumnSpan="2" Text="Project Files" Style="{StaticResource CardTitle}"/>
                    <TextBlock Grid.Row="1" Grid.ColumnSpan="2"
                               Style="{StaticResource MutedText}"
                               Margin="0,6,0,14"
                               Text="Choose the source LAS, TerraPhoto calibration, IML, the folder with all images, and where to save the result."/>

                    <TextBlock Grid.Row="2" Grid.ColumnSpan="2" Text="Point Cloud (.LAS)" Style="{StaticResource BodyText}"/>
                    <TextBox Grid.Row="3" x:Name="LasTextBox" Margin="0,6,10,12"/>
                    <Button Grid.Row="3" Grid.Column="1" x:Name="LasBrowseButton" Style="{StaticResource GhostButton}" Content="Browse" Width="96"/>

                    <TextBlock Grid.Row="4" Grid.ColumnSpan="2" Text="Camera Calibration (.cal)" Style="{StaticResource BodyText}"/>
                    <TextBox Grid.Row="5" x:Name="CalTextBox" Margin="0,6,10,12"/>
                    <Button Grid.Row="5" Grid.Column="1" x:Name="CalBrowseButton" Style="{StaticResource GhostButton}" Content="Browse" Width="96"/>

                    <TextBlock Grid.Row="6" Grid.ColumnSpan="2" Text="Image List (.iml)" Style="{StaticResource BodyText}"/>
                    <TextBox Grid.Row="7" x:Name="ImlTextBox" Margin="0,6,10,12"/>
                    <Button Grid.Row="7" Grid.Column="1" x:Name="ImlBrowseButton" Style="{StaticResource GhostButton}" Content="Browse" Width="96"/>

                    <TextBlock Grid.Row="8" Grid.ColumnSpan="2" Text="Images Folder" Style="{StaticResource BodyText}"/>
                    <TextBox Grid.Row="9" x:Name="ImagesTextBox" Margin="0,6,10,12"/>
                    <Button Grid.Row="9" Grid.Column="1" x:Name="ImagesBrowseButton" Style="{StaticResource GhostButton}" Content="Browse" Width="96"/>

                    <TextBlock Grid.Row="10" Grid.ColumnSpan="2" Text="Output LAS" Style="{StaticResource BodyText}"/>
                    <TextBox Grid.Row="11" x:Name="OutputTextBox" Margin="0,6,10,0"/>
                    <Button Grid.Row="11" Grid.Column="1" x:Name="OutputBrowseButton" Style="{StaticResource GhostButton}" Content="Browse" Width="96"/>
                </Grid>
            </Border>

            <Border Grid.Row="1" Style="{StaticResource CardBorder}">
                <Grid>
                    <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="*"/>
                    </Grid.ColumnDefinitions>
                    <Grid.RowDefinitions>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="Auto"/>
                    </Grid.RowDefinitions>

                    <TextBlock Grid.Row="0" Grid.ColumnSpan="2" Text="Processing Profile" Style="{StaticResource CardTitle}"/>
                    <TextBlock Grid.Row="1" Grid.ColumnSpan="2"
                               Style="{StaticResource MutedText}"
                               Margin="0,6,0,14"
                               Text="Pick a preset and tune a couple of practical runtime settings."/>

                    <StackPanel Grid.Row="2" Grid.Column="0" Margin="0,0,10,0">
                        <TextBlock Text="Profile" Style="{StaticResource BodyText}"/>
                        <ComboBox x:Name="ProfileComboBox" Margin="0,6,0,0" DisplayMemberPath="Title" SelectedValuePath="Key"/>
                    </StackPanel>

                    <StackPanel Grid.Row="2" Grid.Column="1">
                        <TextBlock Text="Chunk Size" Style="{StaticResource BodyText}"/>
                        <TextBox x:Name="ChunkSizeTextBox" Margin="0,6,0,0"/>
                    </StackPanel>

                    <StackPanel Grid.Row="3" Grid.Column="0" Margin="0,12,10,0">
                        <TextBlock Text="Default Color" Style="{StaticResource BodyText}"/>
                        <TextBox x:Name="DefaultColorTextBox" Margin="0,6,0,0"/>
                    </StackPanel>

                    <TextBlock Grid.Row="3" Grid.Column="1" x:Name="ProfileDescriptionText"
                               Margin="0,12,0,0"
                               Style="{StaticResource MutedText}"/>
                </Grid>
            </Border>

        </Grid>

        <Grid Grid.Row="1" Grid.Column="1">
            <Grid.RowDefinitions>
                <RowDefinition Height="Auto"/>
                <RowDefinition Height="*"/>
            </Grid.RowDefinitions>

            <Border Grid.Row="0" Style="{StaticResource CardBorder}">
                <StackPanel>
                    <TextBlock Text="Run" Style="{StaticResource CardTitle}"/>
                    <TextBlock Margin="0,6,0,12"
                               Style="{StaticResource MutedText}"
                               Text="The window stays responsive while processing. The backend log appears in real time."/>

                    <Grid Margin="0,0,0,14">
                        <Grid.ColumnDefinitions>
                            <ColumnDefinition Width="*"/>
                            <ColumnDefinition Width="*"/>
                        </Grid.ColumnDefinitions>
                        <Button Grid.Column="0" x:Name="RunButton" Style="{StaticResource PrimaryButton}" Content="Start Colorization" Margin="0,0,8,0"/>
                        <Button Grid.Column="1" x:Name="ClearButton" Style="{StaticResource GhostButton}" Content="Clear Log"/>
                    </Grid>

                    <ProgressBar x:Name="ProgressBar" Height="12" IsIndeterminate="False" Margin="0,0,0,10" Background="#E6DFD0"/>
                    <TextBlock x:Name="StatusText" FontFamily="Bahnschrift" FontWeight="Bold" Foreground="#7A261D" Text="Ready"/>
                </StackPanel>
            </Border>

            <Border Grid.Row="1" Style="{StaticResource CardBorder}">
                <StackPanel>
                    <TextBlock Text="Quick Notes" Style="{StaticResource CardTitle}"/>
                    <TextBlock Margin="0,12,0,8" Style="{StaticResource BodyText}" Text="• Use LAS and images from the same flight block."/>
                    <TextBlock Margin="0,0,0,8" Style="{StaticResource BodyText}" Text="• Enhanced is the recommended profile. It runs slower because it builds two passes and blends them."/>
                    <TextBlock Margin="0,0,0,8" Style="{StaticResource BodyText}" Text="• Output LAS must be different from the source LAS."/>
                    <TextBlock Margin="0,0,0,8" Style="{StaticResource BodyText}" Text="• Chunk size controls memory use and speed. Around 500000 to 1000000 is a practical range."/>
                    <TextBlock Margin="0,0,0,8" Style="{StaticResource BodyText}" Text="• The log will show missing images from the IML file if some files are absent in the chosen folder."/>
                </StackPanel>
            </Border>
        </Grid>

        <GridSplitter Grid.Row="2"
                      Grid.Column="0"
                      Grid.ColumnSpan="2"
                      Height="8"
                      HorizontalAlignment="Stretch"
                      VerticalAlignment="Stretch"
                      ResizeDirection="Rows"
                      ResizeBehavior="PreviousAndNext"
                      Background="#D7D1C3"
                      ShowsPreview="True"/>

        <Border Grid.Row="3" Grid.Column="0" Grid.ColumnSpan="2" Style="{StaticResource CardBorder}" Margin="0,0,0,0">
            <Grid>
                <Grid.RowDefinitions>
                    <RowDefinition Height="Auto"/>
                    <RowDefinition Height="*"/>
                </Grid.RowDefinitions>
                <TextBlock Grid.Row="0" Text="Run Log" Style="{StaticResource CardTitle}"/>
                <TextBox Grid.Row="1"
                         x:Name="LogTextBox"
                         Margin="0,12,0,0"
                         MinHeight="160"
                         FontFamily="Consolas"
                         FontSize="12"
                         Background="#FFFDF8"
                         Foreground="#1F2328"
                         BorderBrush="#C8BCA8"
                         IsReadOnly="True"
                         VerticalScrollBarVisibility="Auto"
                         HorizontalScrollBarVisibility="Auto"
                         TextWrapping="NoWrap"
                         AcceptsReturn="True"/>
            </Grid>
        </Border>
    </Grid>
</Window>
"@

[xml]$xamlXml = $xaml
$reader = New-Object System.Xml.XmlNodeReader $xamlXml
$window = [Windows.Markup.XamlReader]::Load($reader)

$controlNames = @(
    "LasTextBox",
    "CalTextBox",
    "ImlTextBox",
    "ImagesTextBox",
    "OutputTextBox",
    "ChunkSizeTextBox",
    "DefaultColorTextBox",
    "ProfileComboBox",
    "ProfileDescriptionText",
    "LogTextBox",
    "StatusText",
    "ProgressBar",
    "RunButton",
    "ClearButton",
    "LasBrowseButton",
    "CalBrowseButton",
    "ImlBrowseButton",
    "ImagesBrowseButton",
    "OutputBrowseButton"
)

$controls = @{}
foreach ($name in $controlNames) {
    $controls[$name] = $window.FindName($name)
}

$APP_TITLE = "RGB Point Cloud Colorizer"
$window.Title = $APP_TITLE

$profileItems = foreach ($profileItem in $script:Profiles.GetEnumerator()) {
    [pscustomobject]@{
        Key = $profileItem.Value.Key
        Title = $profileItem.Value.Title
        Description = $profileItem.Value.Description
    }
}
$controls.ProfileComboBox.ItemsSource = $profileItems

$state = Get-SavedState
$controls.LasTextBox.Text = Get-StateValue $state "las" ""
$controls.CalTextBox.Text = Get-StateValue $state "cal" ""
$controls.ImlTextBox.Text = Get-StateValue $state "iml" ""
$controls.ImagesTextBox.Text = Get-StateValue $state "images_dir" ""
$controls.OutputTextBox.Text = Get-StateValue $state "output" ""
$controls.ChunkSizeTextBox.Text = Get-StateValue $state "chunk_size" "1000000"
$controls.DefaultColorTextBox.Text = Get-StateValue $state "default_color" "0,0,0"

$selectedProfile = Get-StateValue $state "profile" "enhanced"
if (-not $script:Profiles.Contains($selectedProfile)) {
    $selectedProfile = "enhanced"
}
$controls.ProfileComboBox.SelectedValue = $selectedProfile
$controls.ProfileDescriptionText.Text = Get-ProfileDescription $selectedProfile
try {
    $controls.DefaultColorTextBox.Text = ConvertTo-DefaultColor $controls.DefaultColorTextBox.Text
}
catch {
    $controls.DefaultColorTextBox.Text = "0,0,0"
}

$controls.ProfileComboBox.add_SelectionChanged({
    $key = [string]$controls.ProfileComboBox.SelectedValue
    $controls.ProfileDescriptionText.Text = Get-ProfileDescription $key
})

$controls.DefaultColorTextBox.add_LostFocus({
    try {
        $controls.DefaultColorTextBox.Text = ConvertTo-DefaultColor $controls.DefaultColorTextBox.Text
    }
    catch {
    }
})

$controls.LasBrowseButton.add_Click({
    $chosen = Show-OpenDialog -Title "Choose LAS point cloud" -Filter "LAS Files (*.las)|*.las|All Files (*.*)|*.*" -InitialPath $controls.LasTextBox.Text
    if ($chosen) {
        $controls.LasTextBox.Text = $chosen
        if ([string]::IsNullOrWhiteSpace($controls.OutputTextBox.Text)) {
            $controls.OutputTextBox.Text = Get-SuggestedOutputPath $chosen
        }
    }
})

$controls.CalBrowseButton.add_Click({
    $chosen = Show-OpenDialog -Title "Choose camera calibration" -Filter "Calibration Files (*.cal)|*.cal|All Files (*.*)|*.*" -InitialPath $controls.CalTextBox.Text
    if ($chosen) { $controls.CalTextBox.Text = $chosen }
})

$controls.ImlBrowseButton.add_Click({
    $chosen = Show-OpenDialog -Title "Choose image list" -Filter "IML Files (*.iml)|*.iml|All Files (*.*)|*.*" -InitialPath $controls.ImlTextBox.Text
    if ($chosen) { $controls.ImlTextBox.Text = $chosen }
})

$controls.ImagesBrowseButton.add_Click({
    $chosen = Show-FolderDialog -Description "Choose the folder with all source images" -InitialPath $controls.ImagesTextBox.Text
    if ($chosen) { $controls.ImagesTextBox.Text = $chosen }
})

$controls.OutputBrowseButton.add_Click({
    $initial = if ($controls.OutputTextBox.Text) { $controls.OutputTextBox.Text } else { Get-SuggestedOutputPath $controls.LasTextBox.Text }
    $chosen = Show-SaveDialog -Title "Save output LAS as" -InitialPath $initial
    if ($chosen) { $controls.OutputTextBox.Text = $chosen }
})

$controls.ClearButton.add_Click({
    if (-not $script:IsRunning) {
        $controls.LogTextBox.Clear()
    }
})

$controls.RunButton.add_Click({
    if ($script:IsRunning) { return }

    $config = Test-Inputs $controls
    if ($null -eq $config) { return }

    Set-SavedState $controls
    $controls.LogTextBox.Clear()
    Add-LogLine $controls "Profile: $($script:Profiles[$config.profile].Title)"
    Add-LogLine $controls $script:Profiles[$config.profile].Description
    Add-LogLine $controls "LAS: $($config.las)"
    Add-LogLine $controls "Images: $($config.images_dir)"
    Add-LogLine $controls "Output: $($config.output)"
    Add-LogLine $controls ""

    Set-Running $controls $true
    $controls.StatusText.Text = "Running"
    $script:LastProcessError = ""
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
    $script:BackendStdoutFile = Join-Path $script:ProjectRoot ("rgbpoint_backend_" + $stamp + "_out.log")
    $script:BackendStderrFile = Join-Path $script:ProjectRoot ("rgbpoint_backend_" + $stamp + "_err.log")
    $script:BackendStdoutCharsRead = 0
    $script:BackendStderrCharsRead = 0
    $argumentParts = @(
        "-u",
        (Join-Path $script:ProjectRoot "run_colorize_job.py"),
        "--las", $config.las,
        "--cal", $config.cal,
        "--iml", $config.iml,
        "--images-dir", $config.images_dir,
        "--output", $config.output,
        "--profile", $config.profile,
        "--chunk-size", [string]$config.chunk_size,
        "--default-color", $config.default_color
    )
    $argumentLine = ($argumentParts | ForEach-Object { ConvertTo-QuotedArgument $_ }) -join " "
    Add-LogLine $controls ("Backend logs: " + $script:BackendStdoutFile)

    try {
        $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
        if ($null -eq $pythonCmd -or [string]::IsNullOrWhiteSpace($pythonCmd.Source)) {
            throw "Python executable was not found in PATH."
        }

        $script:CurrentProcess = Start-Process `
            -FilePath $pythonCmd.Source `
            -ArgumentList $argumentLine `
            -WorkingDirectory $script:ProjectRoot `
            -PassThru `
            -WindowStyle Hidden `
            -RedirectStandardOutput $script:BackendStdoutFile `
            -RedirectStandardError $script:BackendStderrFile

        if ($null -eq $script:StatusTimer) {
            $script:StatusTimer = New-Object System.Windows.Threading.DispatcherTimer
            $script:StatusTimer.Interval = [TimeSpan]::FromMilliseconds(800)
            $script:StatusTimer.add_Tick({
                Flush-BackendLogs $controls
                if ($null -eq $script:CurrentProcess) { return }
                if (-not $script:CurrentProcess.HasExited) { return }

                try {
                    $script:CurrentProcess.WaitForExit()
                }
                catch {
                }

                Flush-BackendLogs $controls

                $exitCode = $script:CurrentProcess.ExitCode
                $script:StatusTimer.Stop()
                Set-Running $controls $false
                Add-LogLine $controls ""
                Add-LogLine $controls ("Process exit code: " + $exitCode)

                if ($exitCode -eq 0) {
                    $controls.StatusText.Text = "Finished"
                    [System.Windows.MessageBox]::Show(
                        "Processing finished successfully.",
                        $APP_TITLE,
                        [System.Windows.MessageBoxButton]::OK,
                        [System.Windows.MessageBoxImage]::Information
                    ) | Out-Null
                }
                else {
                    $controls.StatusText.Text = "Run failed"
                    $failureDetails = $script:LastProcessError
                    if ([string]::IsNullOrWhiteSpace($failureDetails)) {
                        $failureDetails = Get-LogTail $controls
                    }
                    if (-not [string]::IsNullOrWhiteSpace($failureDetails)) {
                        Add-LogLine $controls ("Last error: " + $failureDetails)
                        [System.Windows.MessageBox]::Show(
                            "Processing failed:`n`n$failureDetails",
                            $APP_TITLE,
                            [System.Windows.MessageBoxButton]::OK,
                            [System.Windows.MessageBoxImage]::Error
                        ) | Out-Null
                    }
                    else {
                        [System.Windows.MessageBox]::Show(
                            "Processing failed. Check the log for details.",
                            $APP_TITLE,
                            [System.Windows.MessageBoxButton]::OK,
                            [System.Windows.MessageBoxImage]::Error
                        ) | Out-Null
                    }
                }
            })
        }
        $script:StatusTimer.Start()
    }
    catch {
        Set-Running $controls $false
        $controls.StatusText.Text = "Run failed"
        Add-LogLine $controls $_.Exception.Message
        Write-ErrorLog ("Failed to start processing: " + $_.Exception.Message)
        [System.Windows.MessageBox]::Show(
            "Unable to start Python processing. Check the log for details.",
            $APP_TITLE,
            [System.Windows.MessageBoxButton]::OK,
            [System.Windows.MessageBoxImage]::Error
        ) | Out-Null
    }
})

$window.add_Closing({
    if ($script:IsRunning -and $null -ne $script:CurrentProcess -and -not $script:CurrentProcess.HasExited) {
        $result = [System.Windows.MessageBox]::Show(
            "Colorization is still running. Close the window and stop processing?",
            $APP_TITLE,
            [System.Windows.MessageBoxButton]::YesNo,
            [System.Windows.MessageBoxImage]::Warning
        )
        if ($result -ne [System.Windows.MessageBoxResult]::Yes) {
            $_.Cancel = $true
            return
        }
        try {
            $script:CurrentProcess.Kill()
        }
        catch {
        }
    }
    Set-SavedState $controls
})

if ($SmokeTest) {
    "gui ok"
    return
}

Write-RunTrace "GUI started"
try {
    [void]$window.ShowDialog()
}
catch {
    $details = $_.Exception.ToString()
    Write-ErrorLog ("Unhandled GUI exception: " + $details)
    [System.Windows.MessageBox]::Show(
        "GUI crashed. See rgbpoint_gui_crash.log in the project folder.",
        $APP_TITLE,
        [System.Windows.MessageBoxButton]::OK,
        [System.Windows.MessageBoxImage]::Error
    ) | Out-Null
    throw
}
finally {
    Write-RunTrace "GUI closed"
}
