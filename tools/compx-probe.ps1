# Compx 2.4 GHz receiver probe (3554:FA09: AULA, Redragon, MCHOSE, VXE ...), version 4.
# It lists the receiver's HID collections, reads its status feature report (0x06), and listens to the
# 0x13 channel. The only thing it ever sends is the battery query 13 4A 01 00 00 .. 00 5E (the read
# that the official MCHOSE M HUB driver uses on this receiver), 4 times, 20 s apart. No settings,
# lighting or firmware commands exist in this script.
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File .\compx-probe.ps1
# Close Taskbar Battery Widget first. Keep the keyboard in 2.4 GHz mode and press a key just before running.

$code = @'
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Win32.SafeHandles;

public static class CompxProbe
{
    [StructLayout(LayoutKind.Sequential)] struct SP_DEVICE_INTERFACE_DATA { public int cbSize; public Guid g; public int Flags; public IntPtr Reserved; }
    [StructLayout(LayoutKind.Sequential)] struct HIDD_ATTRIBUTES { public int Size; public ushort Vid; public ushort Pid; public ushort Ver; }
    [StructLayout(LayoutKind.Sequential)] struct HIDP_CAPS {
        public ushort Usage; public ushort UsagePage; public ushort InLen; public ushort OutLen; public ushort FeatLen;
        [MarshalAs(UnmanagedType.ByValArray, SizeConst = 17)] public ushort[] Reserved;
        public ushort NLink; public ushort NInBtn; public ushort NInVal; public ushort NInIdx;
        public ushort NOutBtn; public ushort NOutVal; public ushort NOutIdx; public ushort NFeatBtn; public ushort NFeatVal; public ushort NFeatIdx; }

    [DllImport("hid.dll")] static extern void HidD_GetHidGuid(out Guid g);
    [DllImport("hid.dll")] static extern bool HidD_GetAttributes(SafeFileHandle h, ref HIDD_ATTRIBUTES a);
    [DllImport("hid.dll")] static extern bool HidD_GetPreparsedData(SafeFileHandle h, out IntPtr p);
    [DllImport("hid.dll")] static extern bool HidD_FreePreparsedData(IntPtr p);
    [DllImport("hid.dll")] static extern int HidP_GetCaps(IntPtr p, ref HIDP_CAPS c);
    [DllImport("hid.dll")] static extern int HidP_GetButtonCaps(int type, byte[] caps, ref ushort len, IntPtr p);
    [DllImport("hid.dll")] static extern int HidP_GetValueCaps(int type, byte[] caps, ref ushort len, IntPtr p);
    [DllImport("hid.dll", SetLastError = true)] static extern bool HidD_GetFeature(SafeFileHandle h, byte[] b, int len);
    [DllImport("hid.dll", SetLastError = true)] static extern bool HidD_GetInputReport(SafeFileHandle h, byte[] b, int len);
    [DllImport("setupapi.dll")] static extern IntPtr SetupDiGetClassDevs(ref Guid g, IntPtr e, IntPtr w, uint f);
    [DllImport("setupapi.dll")] static extern bool SetupDiEnumDeviceInterfaces(IntPtr s, IntPtr d, ref Guid g, int i, ref SP_DEVICE_INTERFACE_DATA data);
    [DllImport("setupapi.dll", CharSet = CharSet.Unicode)] static extern bool SetupDiGetDeviceInterfaceDetail(IntPtr s, ref SP_DEVICE_INTERFACE_DATA d, IntPtr detail, int size, out int req, IntPtr info);
    [DllImport("setupapi.dll")] static extern bool SetupDiDestroyDeviceInfoList(IntPtr s);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)] static extern SafeFileHandle CreateFile(string n, uint a, uint s, IntPtr sa, uint c, uint f, IntPtr t);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool WriteFile(SafeFileHandle h, byte[] b, int n, out int written, IntPtr ovl);

    // The one and only packet this probe can send: the battery query, padded to the report length.
    static byte[] BatteryQuery(int len)
    {
        byte[] b = new byte[len];
        b[0] = 0x13; b[1] = 0x4A; b[2] = 0x01;
        b[len - 1] = 0x5E;   // sum of the bytes before it
        return b;
    }

    static string Send(string path, int outLen)
    {
        SafeFileHandle w = CreateFile(path, 0x40000000, 3, IntPtr.Zero, 3, 0, IntPtr.Zero);
        if (w.IsInvalid) return "open for writing failed, error " + Marshal.GetLastWin32Error();
        byte[] q = BatteryQuery(outLen);
        int n = 0; int err = 0;
        Task<bool> t = Task.Run(() => { bool ok = WriteFile(w, q, q.Length, out n, IntPtr.Zero); if (!ok) err = Marshal.GetLastWin32Error(); return ok; });
        string r = !t.Wait(2000) ? "write timed out" : (t.Result ? "sent " + Hex(q, q.Length) : "write failed, error " + err);
        w.Dispose();
        return r;
    }

    static readonly StringBuilder Log = new StringBuilder();
    static void W(string s) { Log.AppendLine(s); Console.WriteLine(s); }
    static string Hex(byte[] b, int n) { return BitConverter.ToString(b, 0, n).Replace("-", " "); }

    static List<string> Paths()
    {
        List<string> r = new List<string>();
        Guid g; HidD_GetHidGuid(out g);
        IntPtr set = SetupDiGetClassDevs(ref g, IntPtr.Zero, IntPtr.Zero, 0x12);
        for (int i = 0; ; i++)
        {
            SP_DEVICE_INTERFACE_DATA d = new SP_DEVICE_INTERFACE_DATA(); d.cbSize = Marshal.SizeOf(typeof(SP_DEVICE_INTERFACE_DATA));
            if (!SetupDiEnumDeviceInterfaces(set, IntPtr.Zero, ref g, i, ref d)) break;
            int req; SetupDiGetDeviceInterfaceDetail(set, ref d, IntPtr.Zero, 0, out req, IntPtr.Zero);
            IntPtr buf = Marshal.AllocHGlobal(req);
            Marshal.WriteInt32(buf, IntPtr.Size == 8 ? 8 : 6);
            if (SetupDiGetDeviceInterfaceDetail(set, ref d, buf, req, out req, IntPtr.Zero)) r.Add(Marshal.PtrToStringUni(new IntPtr(buf.ToInt64() + 4)));
            Marshal.FreeHGlobal(buf);
        }
        SetupDiDestroyDeviceInfoList(set);
        return r;
    }

    static string ReportIds(IntPtr p, int type, int nBtn, int nVal)
    {
        SortedSet<int> ids = new SortedSet<int>();
        if (nBtn > 0) { byte[] b = new byte[72 * nBtn]; ushort n = (ushort)nBtn; HidP_GetButtonCaps(type, b, ref n, p); for (int i = 0; i < n; i++) ids.Add(b[i * 72 + 2]); }
        if (nVal > 0) { byte[] b = new byte[72 * nVal]; ushort n = (ushort)nVal; HidP_GetValueCaps(type, b, ref n, p); for (int i = 0; i < n; i++) ids.Add(b[i * 72 + 2]); }
        List<string> s = new List<string>(); foreach (int id in ids) s.Add("0x" + id.ToString("X2"));
        return s.Count == 0 ? "-" : string.Join(",", s.ToArray());
    }

    public static string Run()
    {
        foreach (string path in Paths())
        {
            if (path.ToLowerInvariant().IndexOf("vid_3554") < 0) continue;
            SafeFileHandle q = CreateFile(path, 0, 3, IntPtr.Zero, 3, 0, IntPtr.Zero);
            if (q.IsInvalid) { W("cannot open " + path); continue; }
            HIDD_ATTRIBUTES a = new HIDD_ATTRIBUTES(); a.Size = Marshal.SizeOf(typeof(HIDD_ATTRIBUTES)); HidD_GetAttributes(q, ref a);
            IntPtr p; HIDP_CAPS c = new HIDP_CAPS();
            string ids = "";
            if (HidD_GetPreparsedData(q, out p))
            {
                HidP_GetCaps(p, ref c);
                ids = " ids in=" + ReportIds(p, 0, c.NInBtn, c.NInVal) + " out=" + ReportIds(p, 1, c.NOutBtn, c.NOutVal) + " feat=" + ReportIds(p, 2, c.NFeatBtn, c.NFeatVal);
                HidD_FreePreparsedData(p);
            }
            q.Dispose();
            W(string.Format("{0:X4}:{1:X4} page {2:X4} usage {3:X4} in {4} out {5} feat {6}{7}", a.Vid, a.Pid, c.UsagePage, c.Usage, c.InLen, c.OutLen, c.FeatLen, ids));
            if (c.UsagePage == 0xFF04 && c.FeatLen > 0) Features(path, c.FeatLen);
            if (c.UsagePage == 0xFF02 && c.InLen > 0) Listen(path, c.InLen, c.OutLen);
        }
        return Log.ToString();
    }

    static void Features(string path, int len)
    {
        SafeFileHandle h = CreateFile(path, 0, 3, IntPtr.Zero, 3, 0, IntPtr.Zero);
        if (h.IsInvalid) { W("  open failed, error " + Marshal.GetLastWin32Error()); return; }
        for (int i = 0; i < 3; i++)
        {
            byte[] b = new byte[len]; b[0] = 0x06;
            bool ok = HidD_GetFeature(h, b, b.Length);
            W("  GetFeature(0x06) #" + (i + 1) + ": " + (ok ? Hex(b, b.Length) : "failed, error " + Marshal.GetLastWin32Error()));
            Thread.Sleep(300);
        }
        h.Dispose();
    }

    public static int ListenSeconds = 90;
    static readonly int[] QueryAt = { 3, 23, 43, 63 };

    static void Listen(string path, int inLen, int outLen)
    {
        SafeFileHandle h = CreateFile(path, 0x80000000, 3, IntPtr.Zero, 3, 0x40000000, IntPtr.Zero);
        if (h.IsInvalid) { W("  open for reading failed, error " + Marshal.GetLastWin32Error()); return; }
        byte[] gi = new byte[inLen]; gi[0] = 0x13;
        bool gok = HidD_GetInputReport(h, gi, gi.Length);
        W("  GetInputReport(0x13): " + (gok ? Hex(gi, gi.Length) : "failed, error " + Marshal.GetLastWin32Error()));
        Console.WriteLine();
        Console.WriteLine("  LISTENING " + ListenSeconds + "s. Keep typing now and then so the keyboard stays awake.");
        Console.WriteLine("  Around the middle, plug the charging cable in, wait 5 s, unplug it.");
        using (FileStream fs = new FileStream(h, FileAccess.Read, inLen, true))
        {
            DateTime start = DateTime.UtcNow;
            DateTime end = start.AddSeconds(ListenSeconds);
            byte[] buf = new byte[inLen];
            int count = 0, next = 0;
            Task<int> t = null;
            while (DateTime.UtcNow < end)
            {
                double now = (DateTime.UtcNow - start).TotalSeconds;
                if (next < QueryAt.Length && now >= QueryAt[next])
                {
                    W(string.Format("    +{0:0.0}s query #{1}: {2}", now, next + 1, outLen >= 20 ? Send(path, outLen) : "not sent (output report too short)"));
                    next++;
                }
                try
                {
                    if (t == null) t = fs.ReadAsync(buf, 0, buf.Length);
                    DateTime until = next < QueryAt.Length ? start.AddSeconds(QueryAt[next]) : end;
                    if (until > end) until = end;
                    int ms = (int)Math.Max(1, (until - DateTime.UtcNow).TotalMilliseconds);
                    if (!t.Wait(ms)) continue;
                    count++;
                    W(string.Format("    +{0:0.0}s {1}", (DateTime.UtcNow - start).TotalSeconds, Hex(buf, t.Result)));
                    t = null;
                }
                catch (Exception ex) { W("    read error: " + ex.GetBaseException().Message); break; }
            }
            W("  heard " + count + " reports on 0x13");
        }
    }
}
'@

Add-Type -TypeDefinition $code
Write-Host "Probing the Compx dongle. It only sends the battery query 13 4A. Press a key on the keyboard now; follow the LISTENING prompt." -ForegroundColor Cyan
Start-Sleep -Seconds 2
$out = [CompxProbe]::Run()
$file = Join-Path ([Environment]::GetFolderPath('Desktop')) 'compx-probe.txt'
$out | Set-Content -Path $file -Encoding UTF8
Write-Host ""
Write-Host "Saved $file - send it back in the thread." -ForegroundColor Green
Read-Host "Press Enter to close" | Out-Null
