using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Windows.Forms;

namespace VoucherManagementUninstall
{
    internal static class Program
    {
        [STAThread]
        private static int Main(string[] args)
        {
            bool quiet = HasFlag(args, "quiet") || HasFlag(args, "verysilent");
            bool removeData = HasFlag(args, "RemoveData");
            string installRoot = Path.GetDirectoryName(
                Assembly.GetExecutingAssembly().Location
            ) ?? String.Empty;

            if (String.IsNullOrWhiteSpace(installRoot))
            {
                return 1;
            }

            if (!quiet)
            {
                DialogResult confirm = MessageBox.Show(
                    "Disinstallare Voucher Management da questa postazione?",
                    "Voucher Management - Disinstallazione",
                    MessageBoxButtons.YesNo,
                    MessageBoxIcon.Question,
                    MessageBoxDefaultButton.Button2
                );
                if (confirm != DialogResult.Yes)
                {
                    return 2;
                }

                DialogResult dataChoice = MessageBox.Show(
                    "Rimuovere anche i dati condivisi dell'applicazione?\r\n\r\n" +
                    "Sì: rimuove configurazione, database, storico locale e file gestiti.\r\n" +
                    "No: disinstalla il programma ma conserva i dati.\r\n" +
                    "Annulla: interrompe la disinstallazione.",
                    "Voucher Management - Dati",
                    MessageBoxButtons.YesNoCancel,
                    MessageBoxIcon.Warning,
                    MessageBoxDefaultButton.Button2
                );
                if (dataChoice == DialogResult.Cancel)
                {
                    return 2;
                }
                removeData = dataChoice == DialogResult.Yes;
            }

            try
            {
                string script = Path.Combine(
                    installRoot,
                    "Uninstall-VoucherManagement.ps1"
                );
                if (!File.Exists(script))
                {
                    throw new InvalidOperationException(
                        "Script di disinstallazione non trovato."
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
                        "Windows PowerShell non è disponibile."
                    );
                }

                StringBuilder psArgs = new StringBuilder();
                psArgs.Append("-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ");
                psArgs.Append(Quote(script));
                psArgs.Append(" -InstallRoot ");
                psArgs.Append(Quote(installRoot));
                if (removeData)
                {
                    psArgs.Append(" -RemoveData");
                }

                ProcessStartInfo startInfo = new ProcessStartInfo();
                startInfo.FileName = powershell;
                startInfo.Arguments = psArgs.ToString();
                startInfo.UseShellExecute = false;
                startInfo.CreateNoWindow = true;
                startInfo.RedirectStandardOutput = true;
                startInfo.RedirectStandardError = true;
                startInfo.WorkingDirectory = installRoot;

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
                            "Impossibile avviare la disinstallazione."
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
                        "Disinstallazione non completata (codice " + exitCode + ").\r\n\r\n" +
                        LimitText(standardError + "\r\n" + standardOutput, 3500)
                    );
                }

                if (!quiet)
                {
                    MessageBox.Show(
                        removeData
                            ? "Voucher Management e i dati condivisi sono stati rimossi."
                            : "Voucher Management è stato rimosso. I dati condivisi sono stati conservati.",
                        "Voucher Management - Disinstallazione",
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Information
                    );
                }
                return 0;
            }
            catch (Exception ex)
            {
                if (!quiet)
                {
                    MessageBox.Show(
                        LimitText(ex.Message, 4000),
                        "Voucher Management - Errore disinstallazione",
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Error
                    );
                }
                return 1;
            }
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

        private static string Quote(string value)
        {
            if (value.IndexOf('"') >= 0)
            {
                throw new ArgumentException(
                    "Un percorso di disinstallazione contiene un carattere non valido."
                );
            }
            return "\"" + value + "\"";
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
    }
}
