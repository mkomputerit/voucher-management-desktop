using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Text;
using System.Windows.Forms;

namespace VoucherManagementSetup
{
    internal static class Program
    {
        private const string PayloadResource = "VoucherManagement.Payload.zip";

        [STAThread]
        private static int Main(string[] args)
        {
            Dictionary<string, string> options = ParseOptions(args);
            bool quiet = HasFlag(args, "quiet") || HasFlag(args, "verysilent");
            string logPath = GetOption(options, "LogPath", String.Empty);
            string installRoot = GetOption(
                options,
                "InstallRoot",
                Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
                    "Voucher Management"
                )
            );
            string defaultDataRoot = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
                "VoucherManagement"
            );
            bool explicitDataRoot =
                options.ContainsKey("DataRoot") &&
                !String.IsNullOrWhiteSpace(options["DataRoot"]);
            string dataRoot = GetOption(
                options,
                "DataRoot",
                defaultDataRoot
            );
            string dataRootDisplay = explicitDataRoot
                ? dataRoot
                : dataRoot + " (nuova installazione; in aggiornamento viene mantenuto il percorso esistente)";

            if (!quiet)
            {
                DialogResult answer = MessageBox.Show(
                    "Voucher Management " + Application.ProductVersion + "\r\n\r\n" +
                    "Installazione condivisa per questa postazione.\r\n" +
                    "Programma: " + installRoot + "\r\n" +
                    "Dati condivisi: " + dataRootDisplay + "\r\n\r\n" +
                    "Sono richiesti privilegi di amministratore. Continuare?",
                    "Voucher Management Setup",
                    MessageBoxButtons.YesNo,
                    MessageBoxIcon.Information,
                    MessageBoxDefaultButton.Button1
                );
                if (answer != DialogResult.Yes)
                {
                    return 2;
                }
            }

            // Setup runs elevated. Keep the extracted script/payload below
            // Program Files rather than the invoking user's writable TEMP so a
            // non-elevated process cannot tamper with code before PowerShell
            // executes it with administrative privileges.
            string workRoot = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
                ".VoucherManagementSetup-" + Guid.NewGuid().ToString("N")
            );

            try
            {
                Directory.CreateDirectory(workRoot);
                string zipPath = Path.Combine(workRoot, "payload.zip");
                WritePayload(zipPath);

                string payloadRoot = Path.Combine(workRoot, "VoucherManagement");
                Directory.CreateDirectory(payloadRoot);
                ZipFile.ExtractToDirectory(zipPath, payloadRoot);

                string installer = Path.Combine(
                    payloadRoot,
                    "Install-VoucherManagement.ps1"
                );
                if (!File.Exists(installer))
                {
                    throw new InvalidOperationException(
                        "Il payload non contiene Install-VoucherManagement.ps1."
                    );
                }

                string powershell = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.Windows),
                    "System32",
                    "WindowsPowerShell",
                    "v1.0",
                    "powershell.exe"
                );
                if (!File.Exists(powershell))
                {
                    throw new InvalidOperationException(
                        "Windows PowerShell non e' disponibile."
                    );
                }

                StringBuilder psArgs = new StringBuilder();
                psArgs.Append("-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ");
                psArgs.Append(Quote(installer));
                psArgs.Append(" -SourcePath ");
                psArgs.Append(Quote(payloadRoot));
                psArgs.Append(" -InstallRoot ");
                psArgs.Append(Quote(installRoot));
                if (explicitDataRoot)
                {
                    psArgs.Append(" -DataRoot ");
                    psArgs.Append(Quote(dataRoot));
                }
                if (HasFlag(args, "SkipShortcut"))
                {
                    psArgs.Append(" -SkipShortcut");
                }

                ProcessStartInfo startInfo = new ProcessStartInfo();
                startInfo.FileName = powershell;
                startInfo.Arguments = psArgs.ToString();
                startInfo.UseShellExecute = false;
                startInfo.CreateNoWindow = true;
                startInfo.RedirectStandardOutput = true;
                startInfo.RedirectStandardError = true;
                startInfo.WorkingDirectory = payloadRoot;

                // Do not let an elevated Windows PowerShell inherit a
                // caller-controlled/user PowerShell module path. The installer
                // only needs trusted Windows/Program Files modules, including
                // Microsoft.PowerShell.Security for Get-Acl.
                string systemModulePath = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.Windows),
                    "System32",
                    "WindowsPowerShell",
                    "v1.0",
                    "Modules"
                );
                string programModulePath = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
                    "WindowsPowerShell",
                    "Modules"
                );
                startInfo.EnvironmentVariables["PSModulePath"] =
                    systemModulePath + ";" + programModulePath;

                string standardOutput;
                string standardError;
                int exitCode;
                using (Process process = Process.Start(startInfo))
                {
                    if (process == null)
                    {
                        throw new InvalidOperationException(
                            "Impossibile avviare Windows PowerShell."
                        );
                    }
                    standardOutput = process.StandardOutput.ReadToEnd();
                    standardError = process.StandardError.ReadToEnd();
                    process.WaitForExit();
                    exitCode = process.ExitCode;
                }

                if (exitCode != 0)
                {
                    throw new InvalidOperationException(
                        "Installazione non completata (codice " + exitCode + ").\r\n\r\n" +
                        LimitText(standardError + "\r\n" + standardOutput, 3500)
                    );
                }

                WriteDiagnosticLog(
                    logPath,
                    "SUCCESS\r\n" + standardOutput
                );
                if (!quiet)
                {
                    MessageBox.Show(
                        "Voucher Management e' stato installato correttamente.\r\n\r\n" +
                        "Se l'utente Windows e' stato appena aggiunto al gruppo operatori, " +
                        "disconnettersi e accedere nuovamente prima del primo avvio.",
                        "Voucher Management Setup",
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Information
                    );
                }
                return 0;
            }
            catch (Exception ex)
            {
                WriteDiagnosticLog(logPath, "ERROR\r\n" + ex.ToString());
                if (!quiet)
                {
                    MessageBox.Show(
                        LimitText(ex.Message, 4000),
                        "Voucher Management Setup - errore",
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Error
                    );
                }
                return 1;
            }
            finally
            {
                TryDeleteDirectory(workRoot);
            }
        }

        private static void WritePayload(string target)
        {
            Assembly assembly = Assembly.GetExecutingAssembly();
            using (Stream input = assembly.GetManifestResourceStream(PayloadResource))
            {
                if (input == null)
                {
                    throw new InvalidOperationException(
                        "Payload incorporato non disponibile."
                    );
                }
                using (FileStream output = File.Create(target))
                {
                    input.CopyTo(output);
                }
            }
        }

        private static Dictionary<string, string> ParseOptions(string[] args)
        {
            Dictionary<string, string> result =
                new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (string raw in args)
            {
                if (String.IsNullOrWhiteSpace(raw))
                {
                    continue;
                }
                string value = raw.Trim();
                while (value.StartsWith("/") || value.StartsWith("-"))
                {
                    value = value.Substring(1);
                }
                int separator = value.IndexOf('=');
                if (separator <= 0)
                {
                    continue;
                }
                string key = value.Substring(0, separator).Trim();
                string optionValue = value.Substring(separator + 1);
                if (key.Length > 0)
                {
                    result[key] = optionValue;
                }
            }
            return result;
        }

        private static bool HasFlag(string[] args, string name)
        {
            foreach (string raw in args)
            {
                string value = (raw ?? String.Empty).Trim();
                while (value.StartsWith("/") || value.StartsWith("-"))
                {
                    value = value.Substring(1);
                }
                if (String.Equals(value, name, StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }
            return false;
        }

        private static string GetOption(
            Dictionary<string, string> options,
            string name,
            string defaultValue
        )
        {
            string value;
            if (options.TryGetValue(name, out value) && !String.IsNullOrWhiteSpace(value))
            {
                return value.Trim();
            }
            return defaultValue;
        }

        private static string Quote(string value)
        {
            if (value.IndexOf('"') >= 0)
            {
                throw new ArgumentException(
                    "Un parametro di installazione contiene un carattere non valido."
                );
            }
            return "\"" + value + "\"";
        }

        private static void WriteDiagnosticLog(string path, string value)
        {
            if (String.IsNullOrWhiteSpace(path))
            {
                return;
            }
            try
            {
                string fullPath = Path.GetFullPath(path);
                string parent = Path.GetDirectoryName(fullPath);
                if (!String.IsNullOrWhiteSpace(parent))
                {
                    Directory.CreateDirectory(parent);
                }
                File.WriteAllText(
                    fullPath,
                    value ?? String.Empty,
                    new UTF8Encoding(false)
                );
            }
            catch
            {
                // Diagnostics must never change the installer outcome.
            }
        }

        private static string LimitText(string value, int maximum)
        {
            string text = (value ?? String.Empty).Trim();
            if (text.Length <= maximum)
            {
                return text;
            }
            return text.Substring(0, maximum) + "\r\n...";
        }

        private static void TryDeleteDirectory(string path)
        {
            if (String.IsNullOrWhiteSpace(path) || !Directory.Exists(path))
            {
                return;
            }
            try
            {
                Directory.Delete(path, true);
            }
            catch
            {
                // Temporary setup data contains only the already-distributed
                // application payload. Windows temp cleanup may remove a file
                // later if antivirus still has a short-lived handle open.
            }
        }
    }
}
