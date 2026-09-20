using System.Text.RegularExpressions;

namespace Platform.TestSupport;

// Locates files of the repository from a test's output folder, so tests use the very same
// realm file and version pins that the deployment uses.
public static partial class RepositoryFiles
{
    public static string Root
    {
        get
        {
            for (var directory = new DirectoryInfo(AppContext.BaseDirectory); directory is not null; directory = directory.Parent)
            {
                if (File.Exists(Path.Combine(directory.FullName, "deploy", "versions.yaml")))
                {
                    return directory.FullName;
                }
            }

            throw new InvalidOperationException("Repository root (deploy/versions.yaml) not found.");
        }
    }

    public static string RealmFile => Path.Combine(Root, "deploy", "charts", "identity", "files", "transaction-platform-realm.json");

    // Reads one pinned image from deploy/versions.yaml, e.g. "keycloak".
    public static string PinnedImage(string key)
    {
        var manifest = File.ReadAllText(Path.Combine(Root, "deploy", "versions.yaml"));
        var match = Regex.Match(manifest, $@"^\s+{Regex.Escape(key)}:\s*(\S+)\s*$", RegexOptions.Multiline);
        return match.Success ? match.Groups[1].Value : throw new InvalidOperationException($"No pinned image '{key}'.");
    }
}
