' 知识库 · 启动控制台（隐藏黑窗）启动器
' 作用：以隐藏窗口方式调用 资料预处理\控制台\启动控制台.bat，避免弹出命令行黑窗
'      启动失败时读取 bat 写出的错误说明文件并弹窗显示（成功后默认程序）
Option Explicit
Dim sh, fso, base, bat, errFile, code, msg
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
bat = base & "\资料预处理\控制台\启动控制台.bat"
errFile = sh.ExpandEnvironmentStrings("%TEMP%") & "\知识库_启动错误.txt"
If fso.FileExists(errFile) Then fso.DeleteFile errFile, True
sh.CurrentDirectory = base
code = sh.Run("cmd /c " & Chr(34) & Chr(34) & bat & Chr(34) & " hidden" & Chr(34), 0, True)
If code <> 0 Then
    If fso.FileExists(errFile) Then
        msg = fso.OpenTextFile(errFile, 1, False, 0).ReadAll()
        fso.DeleteFile errFile, True
    Else
        msg = "启动失败，退出码 " & code & "。请重新双击本文件重试。"
    End If
    MsgBox msg, 16, "知识库 · 启动失败"
End If
