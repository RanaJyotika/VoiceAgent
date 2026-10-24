/**
 * Call recordings into a Google Drive folder, with no Drive API and no app (owner, 24 Oct 2026).
 *
 * Paste this at https://script.google.com (New project), signed in with a Google account that can add files to the
 * folder. Put the same password here (SECRET) and in the project's .env (GDRIVE_SCRIPT_SECRET). Then:
 *   Deploy -> New deployment -> type "Web app" -> Execute as: Me -> Who has access: Anyone -> Deploy -> Allow access.
 * Copy the "Web app URL" into .env as GDRIVE_SCRIPT_URL and restart the server (app/drive_sync.py sends each
 * recording here). The script makes a folder for the call's day ("24 Oct 2026") inside FOLDER_ID and saves the file.
 * "Anyone" only means the address can be called: without the password nothing is saved.
 */
const FOLDER_ID = '1kUs4MRdXLUvSAEJiGncmG1Ps3rEZnvcm';
const SECRET = 'PASTE-THE-PASSWORD-HERE';

function doPost(e) {
  try {
    const req = JSON.parse(e.postData.contents);
    if (req.secret !== SECRET) return reply({ ok: false, error: 'wrong password' });
    const lock = LockService.getScriptLock();
    lock.waitLock(30000);   // two uploads at once must not make the day's folder twice
    try {
      const parent = DriveApp.getFolderById(FOLDER_ID);
      const found = parent.getFoldersByName(req.day);
      const day = found.hasNext() ? found.next() : parent.createFolder(req.day);
      const same = day.getFilesByName(req.name);
      if (same.hasNext()) {   // already there (an upload that was retried): not saved twice
        const f = same.next();
        return reply({ ok: true, id: f.getId(), url: f.getUrl(), again: true });
      }
      const blob = Utilities.newBlob(Utilities.base64Decode(req.data), req.mime, req.name);
      const file = day.createFile(blob);
      return reply({ ok: true, id: file.getId(), url: file.getUrl() });
    } finally {
      lock.releaseLock();
    }
  } catch (err) {
    return reply({ ok: false, error: String(err) });
  }
}

/** Opening the address in a browser shows this: a quick check that the deployment works. */
function doGet() {
  return reply({ ok: true, folder: DriveApp.getFolderById(FOLDER_ID).getName() });
}

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
