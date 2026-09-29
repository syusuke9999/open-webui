# Folder archiving

Open a folder's menu in the sidebar and select **Archive Folder**. After confirmation,
the folder, its subfolders, and their chats disappear from the sidebar. Their chats
are also excluded from normal chat searches, including the assistant's `search_chats`
tool. Folder names, hierarchy, prompts, files, sharing settings, and chat contents are
preserved.

To restore a folder, open **Settings → Data → Archived Chats**, find
**Archived Folders**, and select **Unarchive Folder**. Chats archived individually
before the folder was archived remain archived. A subfolder archived separately also
remains archived; restore its parent before restoring that subfolder.

Only the folder owner can archive or restore it. Archiving a shared folder affects
everyone using it, including chats placed there by other users. The confirmation
dialog explains this before the operation.

While a folder is archived, restore it before creating or moving chats or subfolders
into it. Existing automations retain their destination folder, but cannot create a
chat there until the folder is restored. Individual **Unarchive All** only restores
individually archived chats; archived folders have their own restore controls.

Archiving is a visibility feature, not deletion or an access-control boundary for
the chat owner. Existing direct chat links and explicit `archived:true` searches
retain their existing behavior.

The database migration adds nullable archive markers to folders and chats. Existing
records keep their current state. Each folder operation updates its subtree and
chat archive flags in one transaction; the markers distinguish changes made by
that operation from content that was already archived.
