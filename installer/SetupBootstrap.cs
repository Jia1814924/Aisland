// Compiled as an asInvoker GUI executable. Only the child installer's TEMP/TMP
// are changed; no machine/user environment or existing directory ACL is edited.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;

[assembly: AssemblyTitle("Aisland Installation")]
[assembly: AssemblyProduct("Aisland")]
[assembly: AssemblyVersion("__VERSION__.0")]
[assembly: AssemblyFileVersion("__VERSION__.3")]
[assembly: AssemblyInformationalVersion("__VERSION__ installer revision 3")]

internal static class SetupBootstrap
{
    private const string WindowTitle = "Aisland 安装";
    private const string PayloadHash = "__PAYLOAD_SHA256__";
    private const long PayloadLength = __PAYLOAD_LENGTH__L;
    private static int exitCode = 1;
    private static Exception failure;

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CreateDirectoryW(string path, IntPtr securityAttributes);

    [STAThread]
    private static int Main(string[] args)
    {
        bool silent = Array.Exists(args, delegate(string a) {
            return a.Equals("/SILENT", StringComparison.OrdinalIgnoreCase) ||
                   a.Equals("/VERYSILENT", StringComparison.OrdinalIgnoreCase);
        });
        if (silent)
        {
            try { return RunInstaller(args, null); }
            catch (Exception error) { failure = error; }
        }
        else
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            using (Form form = new Form())
            {
                form.Text = WindowTitle;
                form.ClientSize = new Size(420, 116);
                form.FormBorderStyle = FormBorderStyle.FixedDialog;
                form.StartPosition = FormStartPosition.CenterScreen;
                form.ControlBox = false;
                form.AutoScaleMode = AutoScaleMode.Dpi;
                form.Icon = Icon.ExtractAssociatedIcon(Assembly.GetExecutingAssembly().Location);
                Label label = new Label();
                label.Text = "正在准备安装文件，请稍候…";
                label.AutoSize = true;
                label.Location = new Point(26, 23);
                ProgressBar progress = new ProgressBar();
                progress.Style = ProgressBarStyle.Marquee;
                progress.MarqueeAnimationSpeed = 30;
                progress.SetBounds(26, 64, 368, 13);
                form.Controls.Add(label);
                form.Controls.Add(progress);
                form.Shown += delegate {
                    Task.Factory.StartNew(delegate {
                        try
                        {
                            exitCode = RunInstaller(args, delegate {
                                form.BeginInvoke((Action)delegate { form.Hide(); });
                            });
                        }
                        catch (Exception error) { failure = error; }
                        finally { form.BeginInvoke((Action)delegate { form.Close(); }); }
                    });
                };
                Application.Run(form);
            }
        }
        if (failure != null)
        {
            if (!silent)
                MessageBox.Show("安装文件准备未完成。请将安装文件放到你可以写入的文件夹后重试。\r\n\r\n" +
                    failure.Message, WindowTitle, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        return exitCode;
    }

    private static void CheckDirectory(string path)
    {
        if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
            throw new IOException("安装缓存不能使用目录链接：" + path);
    }

    private static string ChooseCache(string originalDirectory)
    {
        string explicitCache = Environment.GetEnvironmentVariable("AISLAND_SETUP_CACHE");
        List<string> candidates = new List<string>();
        if (!String.IsNullOrWhiteSpace(explicitCache))
            candidates.Add(explicitCache);
        else
        {
            candidates.Add(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "AislandSetupCache"));
            candidates.Add(Path.Combine(originalDirectory, ".aisland-install-cache"));
        }
        Exception lastError = null;
        foreach (string candidate in candidates)
        {
            try
            {
                string cache = Path.GetFullPath(candidate);
                Directory.CreateDirectory(cache);
                CheckDirectory(cache);
                string probe = Path.Combine(cache, ".write-check-" + Guid.NewGuid().ToString("N"));
                using (FileStream stream = new FileStream(probe, FileMode.CreateNew, FileAccess.Write, FileShare.None)) { }
                File.Delete(probe);
                return cache;
            }
            catch (Exception error) { lastError = error; }
        }
        throw new IOException("无法创建本次安装使用的缓存目录。", lastError);
    }

    private static string CreateSession(string parent)
    {
        Directory.CreateDirectory(parent);
        CheckDirectory(parent);
        for (int attempt = 0; attempt < 3; attempt++)
        {
            string path = Path.GetFullPath(Path.Combine(parent, Guid.NewGuid().ToString("N")));
            if (CreateDirectoryW(path, IntPtr.Zero))
            {
                CheckDirectory(path);
                return path;
            }
            int error = Marshal.GetLastWin32Error();
            if (error != 183) throw new Win32Exception(error);
        }
        throw new IOException("无法创建唯一安装缓存目录。");
    }

    private static List<string> ForwardArguments(string[] args)
    {
        List<string> result = new List<string>(args);
        if (!Array.Exists(args, delegate(string a) { return a.StartsWith("/LANG=", StringComparison.OrdinalIgnoreCase); }))
            result.Add("/LANG=chinesesimp");
        return result;
    }

    private static string OriginalUserHome(string[] args)
    {
        foreach (string argument in args)
            if (argument.StartsWith("/USERDATA=", StringComparison.OrdinalIgnoreCase))
                return Path.GetFullPath(Environment.ExpandEnvironmentVariables(argument.Substring(10)));
        string specified = Environment.GetEnvironmentVariable("AISLAND_USER_HOME");
        if (String.IsNullOrWhiteSpace(specified))
            specified = Environment.GetEnvironmentVariable("WISLAND_USER_HOME");  // 旧品牌遗留
        if (!String.IsNullOrWhiteSpace(specified))
            return Path.GetFullPath(Environment.ExpandEnvironmentVariables(specified));
        string local = Environment.GetEnvironmentVariable("LOCALAPPDATA");
        if (String.IsNullOrWhiteSpace(local))
            local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string legacy = Path.Combine(local, "Wisland");
        if (File.Exists(Path.Combine(legacy, "config.json")) &&
            !File.Exists(Path.Combine(local, "Aisland", "config.json")))
            return legacy;   // 程序首次启动会把这个目录接管成 Aisland
        return Path.Combine(local, "Aisland");
    }

    // Windows command-line quoting, including paths ending in backslashes.
    private static string QuoteArgument(string argument)
    {
        StringBuilder result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char character in argument)
        {
            if (character == '\\') { slashes++; continue; }
            if (character == '"')
            {
                result.Append('\\', slashes * 2 + 1);
                result.Append('"');
            }
            else
            {
                result.Append('\\', slashes);
                result.Append(character);
            }
            slashes = 0;
        }
        result.Append('\\', slashes * 2);
        result.Append('"');
        return result.ToString();
    }

    private static int RunInstaller(string[] args, Action started)
    {
        string originalDirectory = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        string cache = ChooseCache(originalDirectory);
        // The stable temp directory remains available to an application launched
        // by the installer. Only the unique payload session is cleaned below.
        string temp = Path.Combine(cache, "Temp");
        Directory.CreateDirectory(temp);
        CheckDirectory(temp);
        string sessions = Path.GetFullPath(Path.Combine(cache, "Packages"));
        string session = CreateSession(sessions);
        string payload = Path.Combine(session, "Aisland-Installer.exe");
        int result = 1;
        try
        {
            using (Stream input = Assembly.GetExecutingAssembly().GetManifestResourceStream("Aisland.SetupPayload"))
            {
                if (input == null) throw new IOException("安装文件中的程序资源缺失。");
                using (FileStream output = new FileStream(payload, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                    input.CopyTo(output, 1024 * 1024);
            }
            // Prevent replacement after verification until the installer exits.
            using (FileStream guard = new FileStream(payload, FileMode.Open, FileAccess.Read, FileShare.Read))
            using (SHA256 digest = SHA256.Create())
            {
                if (guard.Length != PayloadLength ||
                    BitConverter.ToString(digest.ComputeHash(guard)).Replace("-", "") != PayloadHash)
                    throw new IOException("安装文件完整性校验未通过，请重新获取安装文件。");
                List<string> forwarded = ForwardArguments(args);
                string userHome = OriginalUserHome(args);
                if (!Array.Exists(args, delegate(string a) { return a.StartsWith("/USERDATA=", StringComparison.OrdinalIgnoreCase); }))
                    forwarded.Add("/USERDATA=" + userHome);
                List<string> quoted = forwarded.ConvertAll<string>(QuoteArgument);
                ProcessStartInfo info = new ProcessStartInfo(payload, String.Join(" ", quoted.ToArray()));
                info.UseShellExecute = false;
                info.WorkingDirectory = originalDirectory;
                info.EnvironmentVariables["TEMP"] = temp;
                info.EnvironmentVariables["TMP"] = temp;
                // Inno requests UAC itself and retains the original user's token.
                // Both its original-user migration and app launch use this state.
                info.EnvironmentVariables["AISLAND_USER_HOME"] = userHome;
                using (Process child = Process.Start(info))
                {
                    if (child == null) throw new IOException("无法启动安装向导。");
                    if (started != null) started();
                    child.WaitForExit();
                    result = child.ExitCode;
                }
            }
            return result;
        }
        finally
        {
            // Delete only this known payload and empty session directory. Never
            // recurse, remove a shared cache root, or touch user configuration.
            string expectedParent = Path.GetFullPath(sessions).TrimEnd(Path.DirectorySeparatorChar);
            string actualParent = Path.GetDirectoryName(Path.GetFullPath(session));
            if (String.Equals(expectedParent, actualParent, StringComparison.OrdinalIgnoreCase))
            {
                try { if (File.Exists(payload)) File.Delete(payload); } catch { }
                try { Directory.Delete(session, false); } catch { }
            }
        }
    }
}
