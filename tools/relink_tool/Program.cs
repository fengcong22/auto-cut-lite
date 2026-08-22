using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace AutoCutDraftRelinker
{
    public sealed class RelinkException : Exception
    {
        public RelinkException(string message) : base(message) { }
    }

    public sealed class DocumentUpdate
    {
        public string Path;
        public string OriginalText;
        public string UpdatedText;
        public int ChangeCount;
    }

    public sealed class RelinkPlan
    {
        public string DraftDirectory;
        public string DraftRoot;
        public string ProjectName;
        public List<DocumentUpdate> Documents = new List<DocumentUpdate>();
        public Dictionary<string, string> MaterialTargets =
            new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        public int MaterialPathChanges;
        public int MetadataPathChanges;

        public int TotalChanges
        {
            get { return MaterialPathChanges + MetadataPathChanges; }
        }
    }

    public sealed class RelinkResult
    {
        public string Status;
        public string DraftDirectory;
        public string BackupDirectory;
        public int MaterialCount;
        public int MaterialPathChanges;
        public int MetadataPathChanges;
        public int UpdatedDocumentCount;
        public string CompletedAt;
    }

    public static class RelinkCore
    {
        private static readonly string[] PathFields = { "path", "file_path", "file_Path" };
        private static readonly JavaScriptSerializer Json = NewSerializer();

        private static JavaScriptSerializer NewSerializer()
        {
            JavaScriptSerializer serializer = new JavaScriptSerializer();
            serializer.MaxJsonLength = Int32.MaxValue;
            serializer.RecursionLimit = 512;
            return serializer;
        }

        public static RelinkPlan Analyze(string draftDirectory)
        {
            if (String.IsNullOrWhiteSpace(draftDirectory))
                throw new RelinkException("请选择剪映草稿文件夹。");

            string draft = Path.GetFullPath(draftDirectory.Trim().Trim('"'));
            if (!Directory.Exists(draft))
                throw new RelinkException("草稿文件夹不存在：" + draft);
            RejectReparse(draft, "草稿文件夹");

            string rootContent = Path.Combine(draft, "draft_content.json");
            string metaPath = Path.Combine(draft, "draft_meta_info.json");
            if (!File.Exists(rootContent) || !File.Exists(metaPath))
                throw new RelinkException("所选目录不是完整剪映草稿，缺少 draft_content.json 或 draft_meta_info.json。");

            string localRoot = Path.Combine(draft, "Resources", "local");
            string audioAlgRoot = Path.Combine(draft, "Resources", "audioAlg");
            if (!Directory.Exists(localRoot) && !Directory.Exists(audioAlgRoot))
                throw new RelinkException("草稿内没有 Resources\\local 或 Resources\\audioAlg 本地素材。");
            if (Directory.Exists(localRoot)) RejectReparse(localRoot, "Resources\\local");
            if (Directory.Exists(audioAlgRoot)) RejectReparse(audioAlgRoot, "Resources\\audioAlg");

            RelinkPlan plan = new RelinkPlan();
            plan.DraftDirectory = draft.TrimEnd(Path.DirectorySeparatorChar);
            plan.DraftRoot = Directory.GetParent(plan.DraftDirectory).FullName;
            plan.ProjectName = new DirectoryInfo(plan.DraftDirectory).Name;

            List<string> contentPaths = new List<string>();
            contentPaths.Add(rootContent);
            string timelines = Path.Combine(draft, "Timelines");
            if (Directory.Exists(timelines))
            {
                RejectReparse(timelines, "Timelines");
                foreach (string directory in Directory.GetDirectories(timelines).OrderBy(value => value, StringComparer.OrdinalIgnoreCase))
                {
                    RejectReparse(directory, "时间线目录");
                    string timelineContent = Path.Combine(directory, "draft_content.json");
                    if (File.Exists(timelineContent)) contentPaths.Add(timelineContent);
                }
            }

            foreach (string contentPath in contentPaths)
            {
                Dictionary<string, object> content = ReadObject(contentPath);
                int changes = RewriteContentMaterials(content, plan);
                string original = File.ReadAllText(contentPath, Encoding.UTF8);
                string updated = SerializeObject(content);
                plan.Documents.Add(new DocumentUpdate
                {
                    Path = contentPath,
                    OriginalText = original,
                    UpdatedText = updated,
                    ChangeCount = changes
                });
                plan.MaterialPathChanges += changes;
            }

            if (plan.MaterialTargets.Count == 0)
                throw new RelinkException("草稿 JSON 中没有可验证的本地素材引用。");

            Dictionary<string, object> meta = ReadObject(metaPath);
            int metaChanges = RewriteMetadata(meta, plan);
            plan.MetadataPathChanges = metaChanges;
            plan.Documents.Add(new DocumentUpdate
            {
                Path = metaPath,
                OriginalText = File.ReadAllText(metaPath, Encoding.UTF8),
                UpdatedText = SerializeObject(meta),
                ChangeCount = metaChanges
            });

            ValidateTargets(plan);
            return plan;
        }

        public static RelinkResult Apply(string draftDirectory)
        {
            RelinkPlan plan = Analyze(draftDirectory);
            RelinkResult result = new RelinkResult();
            result.DraftDirectory = plan.DraftDirectory;
            result.MaterialCount = plan.MaterialTargets.Count;
            result.MaterialPathChanges = plan.MaterialPathChanges;
            result.MetadataPathChanges = plan.MetadataPathChanges;
            result.CompletedAt = DateTime.Now.ToString("yyyy-MM-ddTHH:mm:ssK");

            if (plan.TotalChanges == 0)
            {
                result.Status = "already_current";
                result.BackupDirectory = "";
                result.UpdatedDocumentCount = 0;
                return result;
            }

            string backup = CreateBackupDirectory(plan);
            result.BackupDirectory = backup;
            List<DocumentUpdate> changed = plan.Documents.Where(item => item.ChangeCount > 0).ToList();
            result.UpdatedDocumentCount = changed.Count;
            try
            {
                foreach (DocumentUpdate document in changed)
                    WriteAtomic(document.Path, document.UpdatedText);

                RelinkPlan verified = Analyze(plan.DraftDirectory);
                if (verified.TotalChanges != 0)
                    throw new RelinkException("写入后复验失败，仍有未重链路径。");
                if (verified.MaterialTargets.Count != plan.MaterialTargets.Count)
                    throw new RelinkException("写入后素材数量发生变化。");

                result.Status = "pass";
                WriteReceipt(backup, result, plan);
                return result;
            }
            catch
            {
                RestoreBackup(plan, backup);
                throw;
            }
        }

        public static bool IsJianYingRunning()
        {
            return Process.GetProcessesByName("JianyingPro").Length > 0;
        }

        public static string ResultJson(RelinkResult relinkResult)
        {
            Dictionary<string, object> payload = new Dictionary<string, object>();
            payload["status"] = relinkResult.Status;
            payload["draft_directory"] = relinkResult.DraftDirectory;
            payload["backup_directory"] = relinkResult.BackupDirectory;
            payload["material_count"] = relinkResult.MaterialCount;
            payload["material_path_changes"] = relinkResult.MaterialPathChanges;
            payload["metadata_path_changes"] = relinkResult.MetadataPathChanges;
            payload["updated_document_count"] = relinkResult.UpdatedDocumentCount;
            payload["completed_at"] = relinkResult.CompletedAt;
            return Json.Serialize(payload);
        }

        public static string PlanJson(RelinkPlan plan)
        {
            Dictionary<string, object> payload = new Dictionary<string, object>();
            payload["status"] = plan.TotalChanges == 0 ? "already_current" : "needs_relink";
            payload["draft_directory"] = plan.DraftDirectory;
            payload["draft_root"] = plan.DraftRoot;
            payload["material_count"] = plan.MaterialTargets.Count;
            payload["material_path_changes"] = plan.MaterialPathChanges;
            payload["metadata_path_changes"] = plan.MetadataPathChanges;
            payload["document_count"] = plan.Documents.Count;
            return Json.Serialize(payload);
        }

        private static Dictionary<string, object> ReadObject(string path)
        {
            RejectReparse(path, Path.GetFileName(path));
            try
            {
                object payload = Json.DeserializeObject(File.ReadAllText(path, Encoding.UTF8));
                Dictionary<string, object> result = payload as Dictionary<string, object>;
                if (result == null) throw new RelinkException(Path.GetFileName(path) + " 不是 JSON 对象。");
                return result;
            }
            catch (RelinkException) { throw; }
            catch (Exception exception)
            {
                throw new RelinkException(Path.GetFileName(path) + " 不是可读的明文 JSON。如果草稿已被剪映加密，请使用未打开过的原始 ZIP 重新解压。\r\n" + exception.Message);
            }
        }

        private static string SerializeObject(Dictionary<string, object> payload)
        {
            return Json.Serialize(payload) + Environment.NewLine;
        }

        private static int RewriteContentMaterials(Dictionary<string, object> content, RelinkPlan plan)
        {
            object materialsObject;
            if (!content.TryGetValue("materials", out materialsObject))
                throw new RelinkException("draft_content.json 缺少 materials。");
            Dictionary<string, object> materials = materialsObject as Dictionary<string, object>;
            if (materials == null)
                throw new RelinkException("draft_content.json 的 materials 结构无效。");

            int changes = 0;
            foreach (KeyValuePair<string, object> bucket in materials)
            {
                IEnumerable rows = bucket.Value as IEnumerable;
                if (rows == null || bucket.Value is string) continue;
                foreach (object rowObject in rows)
                {
                    Dictionary<string, object> row = rowObject as Dictionary<string, object>;
                    if (row == null) continue;
                    List<string> fields = PathFields.Where(field => row.ContainsKey(field) && !String.IsNullOrWhiteSpace(Convert.ToString(row[field]))).ToList();
                    if (fields.Count == 0) continue;

                    string materialId = FirstString(row, "id", "material_id", "music_id");
                    if (String.IsNullOrWhiteSpace(materialId))
                        throw new RelinkException("本地素材引用缺少素材 ID。");

                    string target = null;
                    foreach (string field in fields)
                    {
                        string candidate = ResolveLocalTarget(plan.DraftDirectory, Convert.ToString(row[field]));
                        if (target != null && !SamePath(target, candidate))
                            throw new RelinkException("同一素材的多个路径字段指向不同文件：" + materialId);
                        target = candidate;
                    }

                    string existing;
                    if (plan.MaterialTargets.TryGetValue(materialId, out existing) && !SamePath(existing, target))
                        throw new RelinkException("同一素材 ID 在多份草稿 JSON 中指向不同文件：" + materialId);
                    plan.MaterialTargets[materialId] = target;

                    foreach (string field in fields)
                    {
                        if (!SamePath(Convert.ToString(row[field]), target))
                        {
                            row[field] = target;
                            changes++;
                        }
                    }
                }
            }
            return changes;
        }

        private static int RewriteMetadata(Dictionary<string, object> meta, RelinkPlan plan)
        {
            int changes = 0;
            changes += SetIfDifferent(meta, "draft_root_path", plan.DraftRoot);
            changes += SetIfDifferent(meta, "draft_fold_path", plan.DraftDirectory);
            changes += SetIfDifferent(meta, "draft_name", plan.ProjectName);

            object bucketsObject;
            if (!meta.TryGetValue("draft_materials", out bucketsObject)) return changes;
            IEnumerable buckets = bucketsObject as IEnumerable;
            if (buckets == null || bucketsObject is string)
                throw new RelinkException("draft_meta_info.json 的 draft_materials 结构无效。");
            foreach (object bucketObject in buckets)
            {
                Dictionary<string, object> bucket = bucketObject as Dictionary<string, object>;
                if (bucket == null || !bucket.ContainsKey("value")) continue;
                IEnumerable rows = bucket["value"] as IEnumerable;
                if (rows == null || bucket["value"] is string) continue;
                foreach (object rowObject in rows)
                {
                    Dictionary<string, object> row = rowObject as Dictionary<string, object>;
                    if (row == null || !row.ContainsKey("file_Path") || String.IsNullOrWhiteSpace(Convert.ToString(row["file_Path"]))) continue;
                    string id = FirstString(row, "id", "material_id");
                    string target;
                    if (String.IsNullOrWhiteSpace(id) || !plan.MaterialTargets.TryGetValue(id, out target))
                        throw new RelinkException("draft_meta_info.json 中的本地素材无法与草稿素材 ID 匹配。");
                    if (!SamePath(Convert.ToString(row["file_Path"]), target))
                    {
                        row["file_Path"] = target;
                        changes++;
                    }
                    string basename = Path.GetFileName(target);
                    if (row.ContainsKey("extra_info") && !String.Equals(Convert.ToString(row["extra_info"]), basename, StringComparison.Ordinal))
                    {
                        row["extra_info"] = basename;
                        changes++;
                    }
                }
            }
            return changes;
        }

        private static string ResolveLocalTarget(string draftDirectory, string originalPath)
        {
            if (String.IsNullOrWhiteSpace(originalPath) || !Path.IsPathRooted(originalPath))
                throw new RelinkException("素材路径不是 Windows 绝对路径：" + originalPath);
            string normalized = originalPath.Replace('/', '\\');
            string[] markers = { "\\Resources\\local\\", "\\Resources\\audioAlg\\" };
            string selected = null;
            int markerIndex = -1;
            foreach (string marker in markers)
            {
                int index = normalized.IndexOf(marker, StringComparison.OrdinalIgnoreCase);
                if (index >= 0)
                {
                    if (selected != null) throw new RelinkException("素材路径包含多个 Resources 标记：" + originalPath);
                    selected = marker;
                    markerIndex = index;
                }
            }
            if (selected == null)
                throw new RelinkException("发现包外素材路径，工具不会自动修改：" + originalPath);

            string relative = normalized.Substring(markerIndex + 1);
            if (relative.Split('\\').Any(part => part == "" || part == "." || part == ".."))
                throw new RelinkException("素材相对路径不安全：" + originalPath);
            string target = Path.GetFullPath(Path.Combine(draftDirectory, relative));
            string allowedRoot = Path.GetFullPath(Path.Combine(draftDirectory, selected.IndexOf("audioAlg", StringComparison.OrdinalIgnoreCase) >= 0 ? "Resources\\audioAlg" : "Resources\\local"));
            if (!IsInside(target, allowedRoot))
                throw new RelinkException("素材路径越出草稿 Resources 目录：" + originalPath);
            if (!File.Exists(target))
                throw new RelinkException("包内素材缺失：" + target);
            RejectReparse(target, "本地素材");
            return target;
        }

        private static void ValidateTargets(RelinkPlan plan)
        {
            foreach (KeyValuePair<string, string> row in plan.MaterialTargets)
            {
                if (!File.Exists(row.Value))
                    throw new RelinkException("素材不存在：" + row.Value);
                string local = Path.Combine(plan.DraftDirectory, "Resources", "local");
                string audio = Path.Combine(plan.DraftDirectory, "Resources", "audioAlg");
                if (!IsInside(row.Value, local) && !IsInside(row.Value, audio))
                    throw new RelinkException("素材不在允许的 Resources 目录：" + row.Value);
            }
        }

        private static string CreateBackupDirectory(RelinkPlan plan)
        {
            string stamp = DateTime.Now.ToString("yyyyMMdd_HHmmss");
            string backupRoot = Path.Combine(plan.DraftDirectory, ".autocut-relink-backup");
            if (Directory.Exists(backupRoot)) RejectReparse(backupRoot, "重链备份目录");
            Directory.CreateDirectory(backupRoot);
            try { File.SetAttributes(backupRoot, File.GetAttributes(backupRoot) | FileAttributes.Hidden); }
            catch { }
            string backup = Path.Combine(backupRoot, stamp);
            if (Directory.Exists(backup) || File.Exists(backup))
                backup = backup + "_" + Guid.NewGuid().ToString("N").Substring(0, 6);
            Directory.CreateDirectory(backup);
            foreach (DocumentUpdate document in plan.Documents.Where(item => item.ChangeCount > 0))
            {
                string relative = RelativePath(plan.DraftDirectory, document.Path);
                string destination = Path.Combine(backup, relative);
                Directory.CreateDirectory(Path.GetDirectoryName(destination));
                File.Copy(document.Path, destination, false);
            }
            return backup;
        }

        private static void RestoreBackup(RelinkPlan plan, string backup)
        {
            foreach (DocumentUpdate document in plan.Documents.Where(item => item.ChangeCount > 0))
            {
                string saved = Path.Combine(backup, RelativePath(plan.DraftDirectory, document.Path));
                if (File.Exists(saved)) File.Copy(saved, document.Path, true);
            }
        }

        private static void WriteAtomic(string path, string content)
        {
            string temporary = path + ".relink-" + Guid.NewGuid().ToString("N") + ".tmp";
            File.WriteAllText(temporary, content, new UTF8Encoding(false));
            try
            {
                File.Replace(temporary, path, null, true);
            }
            catch
            {
                if (File.Exists(temporary)) File.Delete(temporary);
                throw;
            }
        }

        private static void WriteReceipt(string backup, RelinkResult result, RelinkPlan plan)
        {
            Dictionary<string, object> receipt = new Dictionary<string, object>();
            receipt["schema_version"] = 1;
            receipt["status"] = result.Status;
            receipt["draft_directory"] = result.DraftDirectory;
            receipt["backup_directory"] = result.BackupDirectory;
            receipt["material_count"] = result.MaterialCount;
            receipt["material_path_changes"] = result.MaterialPathChanges;
            receipt["metadata_path_changes"] = result.MetadataPathChanges;
            receipt["updated_document_count"] = result.UpdatedDocumentCount;
            receipt["completed_at"] = result.CompletedAt;
            List<object> materials = new List<object>();
            foreach (KeyValuePair<string, string> item in plan.MaterialTargets.OrderBy(item => item.Key, StringComparer.OrdinalIgnoreCase))
            {
                Dictionary<string, object> material = new Dictionary<string, object>();
                material["material_id"] = item.Key;
                material["path"] = item.Value;
                material["byte_size"] = new FileInfo(item.Value).Length;
                material["sha256"] = Sha256(item.Value);
                materials.Add(material);
            }
            receipt["materials"] = materials;
            File.WriteAllText(Path.Combine(backup, "重链结果.json"), Json.Serialize(receipt) + Environment.NewLine, new UTF8Encoding(false));
        }

        private static string Sha256(string path)
        {
            using (SHA256 algorithm = SHA256.Create())
            using (FileStream stream = File.OpenRead(path))
                return BitConverter.ToString(algorithm.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
        }

        private static int SetIfDifferent(Dictionary<string, object> row, string key, string value)
        {
            object current;
            if (row.TryGetValue(key, out current) && SamePath(Convert.ToString(current), value)) return 0;
            row[key] = value;
            return 1;
        }

        private static string FirstString(Dictionary<string, object> row, params string[] fields)
        {
            foreach (string field in fields)
            {
                object value;
                if (row.TryGetValue(field, out value) && !String.IsNullOrWhiteSpace(Convert.ToString(value)))
                    return Convert.ToString(value).Trim();
            }
            return "";
        }

        private static bool SamePath(string left, string right)
        {
            if (String.IsNullOrWhiteSpace(left) || String.IsNullOrWhiteSpace(right)) return false;
            try
            {
                return String.Equals(Path.GetFullPath(left.Replace('/', '\\')).TrimEnd('\\'), Path.GetFullPath(right.Replace('/', '\\')).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase);
            }
            catch { return false; }
        }

        private static bool IsInside(string path, string root)
        {
            string fullPath = Path.GetFullPath(path).TrimEnd('\\') + "\\";
            string fullRoot = Path.GetFullPath(root).TrimEnd('\\') + "\\";
            return fullPath.StartsWith(fullRoot, StringComparison.OrdinalIgnoreCase);
        }

        private static string RelativePath(string root, string path)
        {
            Uri rootUri = new Uri(Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar);
            Uri pathUri = new Uri(Path.GetFullPath(path));
            return Uri.UnescapeDataString(rootUri.MakeRelativeUri(pathUri).ToString()).Replace('/', Path.DirectorySeparatorChar);
        }

        private static void RejectReparse(string path, string label)
        {
            FileAttributes attributes = File.GetAttributes(path);
            if ((attributes & FileAttributes.ReparsePoint) != 0)
                throw new RelinkException(label + " 是链接或重解析点，已拒绝：" + path);
        }
    }

    public sealed class MainForm : Form
    {
        private readonly TextBox pathBox = new TextBox();
        private readonly Label stateLabel = new Label();
        private readonly Button checkButton = new Button();
        private readonly Button relinkButton = new Button();

        public MainForm()
        {
            Text = "Auto-Cut 剪映素材路径重链";
            Font = new Font("微软雅黑", 9F);
            ClientSize = new Size(700, 330);
            MinimumSize = new Size(620, 330);
            StartPosition = FormStartPosition.CenterScreen;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;

            Label title = new Label();
            title.Text = "可选：重链草稿内的本地素材";
            title.Font = new Font("微软雅黑", 14F, FontStyle.Bold);
            title.Location = new Point(24, 20);
            title.AutoSize = true;
            Controls.Add(title);

            Label description = new Label();
            description.Text = "只在目标电脑的剪映草稿路径与原电脑不同时使用。\r\n先把整个草稿文件夹放进目标剪映草稿目录，完全关闭剪映，再运行重链。";
            description.Location = new Point(27, 61);
            description.Size = new Size(645, 48);
            Controls.Add(description);

            Label pathLabel = new Label();
            pathLabel.Text = "草稿文件夹";
            pathLabel.Location = new Point(27, 125);
            pathLabel.AutoSize = true;
            Controls.Add(pathLabel);

            pathBox.Location = new Point(28, 149);
            pathBox.Size = new Size(548, 26);
            pathBox.Text = AutoDetectDraft();
            Controls.Add(pathBox);

            Button browse = new Button();
            browse.Text = "选择...";
            browse.Location = new Point(586, 146);
            browse.Size = new Size(85, 30);
            browse.Click += BrowseClicked;
            Controls.Add(browse);

            stateLabel.Location = new Point(28, 191);
            stateLabel.Size = new Size(643, 54);
            stateLabel.Text = "先点击“检查”，不会修改草稿。";
            Controls.Add(stateLabel);

            checkButton.Text = "检查";
            checkButton.Location = new Point(477, 265);
            checkButton.Size = new Size(92, 36);
            checkButton.Click += CheckClicked;
            Controls.Add(checkButton);

            relinkButton.Text = "备份并重链";
            relinkButton.Location = new Point(579, 265);
            relinkButton.Size = new Size(92, 36);
            relinkButton.Click += RelinkClicked;
            Controls.Add(relinkButton);
        }

        private string AutoDetectDraft()
        {
            string baseDirectory = AppDomain.CurrentDomain.BaseDirectory;
            if (File.Exists(Path.Combine(baseDirectory, "draft_content.json"))) return baseDirectory.TrimEnd('\\');
            string[] candidates = Directory.GetDirectories(baseDirectory)
                .Where(path => File.Exists(Path.Combine(path, "draft_content.json")))
                .ToArray();
            return candidates.Length == 1 ? candidates[0] : "";
        }

        private void BrowseClicked(object sender, EventArgs e)
        {
            using (FolderBrowserDialog dialog = new FolderBrowserDialog())
            {
                dialog.Description = "选择包含 draft_content.json 的剪映草稿文件夹";
                if (Directory.Exists(pathBox.Text)) dialog.SelectedPath = pathBox.Text;
                if (dialog.ShowDialog(this) == DialogResult.OK) pathBox.Text = dialog.SelectedPath;
            }
        }

        private void CheckClicked(object sender, EventArgs e)
        {
            RunBusy(delegate
            {
                RelinkPlan plan = RelinkCore.Analyze(pathBox.Text);
                if (plan.TotalChanges == 0)
                    stateLabel.Text = "检查通过：" + plan.MaterialTargets.Count + " 个本地素材已指向当前草稿目录，无需重链。";
                else
                    stateLabel.Text = "检查通过：找到 " + plan.MaterialTargets.Count + " 个包内素材，需更新 " + plan.TotalChanges + " 个路径字段。";
            });
        }

        private void RelinkClicked(object sender, EventArgs e)
        {
            RunBusy(delegate
            {
                if (RelinkCore.IsJianYingRunning())
                    throw new RelinkException("检测到剪映正在运行。请先保存工作并完全退出剪映，再运行重链。");
                RelinkPlan plan = RelinkCore.Analyze(pathBox.Text);
                if (plan.TotalChanges == 0)
                {
                    stateLabel.Text = "素材已指向当前草稿目录，无需重链。";
                    return;
                }
                DialogResult answer = MessageBox.Show(this,
                    "将更新 " + plan.TotalChanges + " 个路径字段。\r\n原 JSON 会先备份到草稿内的 .autocut-relink-backup。\r\n\r\n继续吗？",
                    "确认重链", MessageBoxButtons.OKCancel, MessageBoxIcon.Question);
                if (answer != DialogResult.OK) return;
                RelinkResult result = RelinkCore.Apply(pathBox.Text);
                stateLabel.Text = "重链完成：" + result.MaterialCount + " 个本地素材已指向当前草稿。\r\n备份：" + result.BackupDirectory;
                MessageBox.Show(this, "重链已完成。现在可以打开剪映验证草稿。", "完成", MessageBoxButtons.OK, MessageBoxIcon.Information);
            });
        }

        private void RunBusy(Action action)
        {
            checkButton.Enabled = false;
            relinkButton.Enabled = false;
            Cursor = Cursors.WaitCursor;
            try { action(); }
            catch (Exception exception)
            {
                stateLabel.Text = "失败：" + exception.Message;
                MessageBox.Show(this, exception.Message, "无法重链", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
            finally
            {
                Cursor = Cursors.Default;
                checkButton.Enabled = true;
                relinkButton.Enabled = true;
            }
        }
    }

    public static class Program
    {
        [STAThread]
        public static int Main(string[] args)
        {
            try
            {
                if (args.Length >= 2 && String.Equals(args[0], "--check", StringComparison.OrdinalIgnoreCase))
                {
                    Console.OutputEncoding = Encoding.UTF8;
                    Console.WriteLine(RelinkCore.PlanJson(RelinkCore.Analyze(args[1])));
                    return 0;
                }
                if (args.Length >= 3 && String.Equals(args[0], "--relink", StringComparison.OrdinalIgnoreCase) && args.Contains("--yes"))
                {
                    Console.OutputEncoding = Encoding.UTF8;
                    if (RelinkCore.IsJianYingRunning())
                        throw new RelinkException("检测到剪映正在运行，已拒绝修改。");
                    Console.WriteLine(RelinkCore.ResultJson(RelinkCore.Apply(args[1])));
                    return 0;
                }
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                Application.Run(new MainForm());
                return 0;
            }
            catch (Exception exception)
            {
                Console.Error.WriteLine(exception.Message);
                return 1;
            }
        }
    }
}
