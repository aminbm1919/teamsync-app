// The eyes TeamSync cannot have from outside the editor.
//
// Windows keeps no record of a file merely being open: code editors read the
// file, close it at once, and hold the text in their own memory - measured,
// not assumed. Only the editor itself knows what is open, what carries unsaved
// typing, and the exact moment of closing. This extension stands inside and
// reports exactly that, into one small file the sync engine reads.
//
// It speaks only inside TeamSync projects (recognised by their marker files),
// and says nothing about anything else.
const vscode = require('vscode');
const fs = require('fs');
const path = require('path');

function teamsyncRoot(folder) {
    const root = folder.uri.fsPath;
    if (fs.existsSync(path.join(root, 'TEAM-PROJECT-REFERENCE.md')) ||
        fs.existsSync(path.join(root, '.teamsync.lock'))) return root;
    return null;
}

function snapshot() {
    const folders = vscode.workspace.workspaceFolders || [];
    for (const folder of folders) {
        const root = teamsyncRoot(folder);
        if (!root) continue;
        const open = [];
        for (const doc of vscode.workspace.textDocuments) {
            if (doc.isUntitled || doc.uri.scheme !== 'file') continue;
            const rel = path.relative(root, doc.uri.fsPath);
            if (rel.startsWith('..') || path.isAbsolute(rel)) continue;
            if (rel.startsWith('.teamsync') || rel.startsWith('_conflicts')) continue;
            open.push({ f: rel.split(path.sep).join('/'), dirty: doc.isDirty });
        }
        const payload = JSON.stringify({ updated: new Date().toISOString(), open });
        try { fs.writeFileSync(path.join(root, '.teamsync-editor.json'), payload); } catch (e) { }
    }
}

function activate(context) {
    // Every open, close and save redraws the picture; a ten-second heartbeat
    // keeps the timestamp fresh so a crashed or closed editor goes stale and
    // the engine stops believing a report nobody is maintaining.
    context.subscriptions.push(vscode.workspace.onDidOpenTextDocument(snapshot));
    context.subscriptions.push(vscode.workspace.onDidCloseTextDocument(snapshot));
    context.subscriptions.push(vscode.workspace.onDidSaveTextDocument(snapshot));
    const timer = setInterval(snapshot, 10000);
    context.subscriptions.push({ dispose: () => clearInterval(timer) });
    snapshot();
}
function deactivate() { }
module.exports = { activate, deactivate };
