<script lang="ts">
	// @ts-ignore
	import fileSaver from 'file-saver';
	const { saveAs } = fileSaver;

	import dayjs from 'dayjs';
	import calendar from 'dayjs/plugin/calendar';
	import localizedFormat from 'dayjs/plugin/localizedFormat';
	import { getContext, onMount } from 'svelte';
	import type { Writable } from 'svelte/store';
	import { goto } from '$app/navigation';
	import { toast } from 'svelte-sonner';

	import {
		archiveChatById,
		deleteChatById,
		getAllArchivedChats,
		getArchivedChatCount,
		getArchivedChatList,
		unarchiveAllChats
	} from '$lib/apis/chats';
	import { getArchivedFolders, updateFolderArchivedById } from '$lib/apis/folders';
	import { chatId, config, showSettings, user } from '$lib/stores';
	import { refreshChatList, refreshSidebar } from '$lib/stores/chatList';
	import { formatNumber } from '$lib/utils';
	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	import Dropdown from '$lib/components/common/Dropdown.svelte';
	import DropdownMenu from '$lib/components/common/DropdownMenu.svelte';
	import Loader from '$lib/components/common/Loader.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import ChevronUp from '$lib/components/icons/ChevronUp.svelte';
	import Download from '$lib/components/icons/Download.svelte';
	import FolderIcon from '$lib/components/icons/Folder.svelte';
	import Search from '$lib/components/icons/Search.svelte';
	import Trash from '$lib/components/icons/Trash.svelte';
	import UndoAction from '$lib/components/icons/UndoAction.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';

	dayjs.extend(localizedFormat);
	dayjs.extend(calendar);

	const i18n: Writable<any> = getContext('i18n');

	let loading = false;
	let chatList: any[] | null = null;
	let chatCount: number | null = null;
	let page = 1;

	let query = '';
	let orderBy = 'updated_at';
	let direction = 'desc';

	let allChatsLoaded = false;
	let chatListLoading = false;
	let searchDebounceTimeout: ReturnType<typeof setTimeout> | null = null;

	let selectedChatId: string | null = null;
	let showDeleteConfirmDialog = false;
	let showUnarchiveAllConfirmDialog = false;
	let archivedFolders: any[] | null = null;
	let foldersLoadFailed = false;
	let restoringFolderId: string | null = null;
	$: foldersEnabled =
		$config?.features?.enable_folders &&
		($user?.role === 'admin' || ($user?.permissions?.features?.folders ?? true));
	$: filteredFolders = (archivedFolders ?? []).filter((folder) =>
		folder.name.toLocaleLowerCase().includes(query.toLocaleLowerCase())
	);

	const getFilter = () => ({
		...(query ? { query } : {}),
		...(orderBy ? { order_by: orderBy } : {}),
		...(direction ? { direction } : {})
	});

	const loadChats = async () => {
		page = 1;
		chatList = null;
		chatList = await getArchivedChatList(localStorage.token, page, getFilter());
		allChatsLoaded = (chatList ?? []).length === 0;
		chatCount = await getArchivedChatCount(localStorage.token);
	};

	const loadFolders = async () => {
		if (!foldersEnabled) return;
		foldersLoadFailed = false;
		try {
			archivedFolders = await getArchivedFolders(localStorage.token);
		} catch (error) {
			foldersLoadFailed = true;
			toast.error(error instanceof Error ? error.message : `${error}`);
		}
	};

	const unarchiveFolderHandler = async (id: string) => {
		if (restoringFolderId || !foldersEnabled) return;
		restoringFolderId = id;
		try {
			await updateFolderArchivedById(localStorage.token, id, false);
			archivedFolders = archivedFolders?.filter((folder) => folder.id !== id) ?? null;
			toast.success($i18n.t('Folder unarchived.'));
			await Promise.all([loadFolders(), loadChats(), refreshSidebar(localStorage.token)]);
		} catch (error) {
			toast.error(
				error instanceof Error && 'status' in error && error.status === 409
					? $i18n.t('Unarchive the parent folder first.')
					: error instanceof Error
						? error.message
						: `${error}`
			);
		} finally {
			restoringFolderId = null;
		}
	};

	const searchHandler = () => {
		if (searchDebounceTimeout) {
			clearTimeout(searchDebounceTimeout);
		}

		if (query === '') {
			loadChats();
		} else {
			searchDebounceTimeout = setTimeout(loadChats, 300);
		}
	};

	const loadMoreChats = async () => {
		chatListLoading = true;
		page += 1;

		const newChatList = await getArchivedChatList(localStorage.token, page, getFilter());
		allChatsLoaded = newChatList.length === 0;

		if (newChatList.length > 0) {
			chatList = [...(chatList || []), ...newChatList];
		}

		chatListLoading = false;
	};

	const setSortKey = async (key: string) => {
		if (orderBy === key) {
			direction = direction === 'asc' ? 'desc' : 'asc';
		} else {
			orderBy = key;
			direction = 'asc';
		}

		await loadChats();
	};

	const exportChatsHandler = async () => {
		const chats = await getAllArchivedChats(localStorage.token);
		const blob = new Blob([JSON.stringify(chats)], {
			type: 'application/json'
		});
		saveAs(blob, `${$i18n.t('archived-chat-export')}-${Date.now()}.json`);
	};

	const unarchiveHandler = async (id: string) => {
		await archiveChatById(localStorage.token, id).catch((error) => {
			toast.error(`${error}`);
		});

		chatList = chatList?.filter((chat) => chat.id !== id) ?? null;
		if (chatCount !== null) chatCount -= 1;
		await refreshChatList(localStorage.token, { refreshPinned: true });
	};

	const deleteHandler = async () => {
		if (!selectedChatId) return;

		const id = selectedChatId;
		await deleteChatById(localStorage.token, id).catch((error) => {
			toast.error(`${error}`);
		});

		chatList = chatList?.filter((chat) => chat.id !== id) ?? null;
		if (chatCount !== null) chatCount -= 1;
		selectedChatId = null;

		if ($chatId === id) {
			await goto('/');
			chatId.set('');
		}

		await refreshChatList(localStorage.token);
	};

	const unarchiveAllHandler = async () => {
		loading = true;
		try {
			const success = await unarchiveAllChats(localStorage.token);
			if (!success) return;
			toast.success($i18n.t('All chats have been unarchived.'));
			await loadChats();
			await refreshSidebar(localStorage.token);
		} catch (error) {
			toast.error(`${error}`);
		} finally {
			loading = false;
		}
	};

	onMount(() => {
		loadChats();
		loadFolders();
	});
</script>

<ConfirmDialog
	bind:show={showDeleteConfirmDialog}
	on:confirm={() => {
		deleteHandler();
	}}
/>

<ConfirmDialog
	bind:show={showUnarchiveAllConfirmDialog}
	message={$i18n.t('Are you sure you want to unarchive all archived chats?')}
	confirmLabel={$i18n.t('settings.personal.archivedChats.unarchiveAll.label')}
	on:confirm={() => {
		unarchiveAllHandler();
	}}
/>

<div id="tab-archived-chats" class="flex flex-col h-full text-sm">
	<div class="mb-3 flex items-center justify-between">
		<h2 class="text-sm font-medium text-gray-900 dark:text-white">
			{$i18n.t('settings.personal.archivedChats.title')}
			{#if chatCount !== null}
				<span class="ml-2 font-normal text-gray-500 dark:text-gray-500">
					{formatNumber(chatCount)}
				</span>
			{/if}
		</h2>
	</div>

	<div class="flex h-7 shrink-0 items-center w-full gap-2">
		<div class="flex min-w-0 flex-1 items-center gap-2">
			<Search className="size-3.5 shrink-0 text-gray-400 dark:text-gray-600" />
			<input
				data-settings-search
				class="min-w-0 flex-1 bg-transparent py-0.5 text-xs text-gray-700 outline-hidden placeholder:text-gray-300 dark:text-gray-300 dark:placeholder:text-gray-700"
				bind:value={query}
				on:input={searchHandler}
				placeholder={$i18n.t('Search')}
				maxlength="500"
			/>
			{#if query}
				<div class="shrink-0">
					<button
						class="rounded-lg p-0.5 text-gray-400 transition-colors hover:text-gray-700 dark:text-gray-600 dark:hover:text-gray-300"
						type="button"
						aria-label={$i18n.t('Clear search')}
						on:click={() => {
							query = '';
							searchHandler();
						}}
					>
						<XMark className="size-3" strokeWidth="2" />
					</button>
				</div>
			{/if}
		</div>

		<Dropdown align="end">
			<Tooltip content={$i18n.t('Actions')}>
				<button
					class="flex h-7 items-center gap-1.5 rounded-lg bg-transparent px-1.5 text-xs text-gray-500 transition-colors hover:text-gray-900 dark:text-gray-500 dark:hover:text-white"
					type="button"
				>
					<span>{$i18n.t('Actions')}</span>
					<ChevronDown className="size-3" strokeWidth="2.5" />
				</button>
			</Tooltip>

			<div slot="content">
				<DropdownMenu className="w-[10.625rem] shadow-sm">
					<button
						class="flex h-[1.6875rem] w-full cursor-pointer select-none items-center gap-2 rounded-lg bg-transparent px-2 text-xs hover:text-gray-900 disabled:cursor-default disabled:opacity-30 dark:hover:text-gray-100"
						disabled={loading || chatCount === 0}
						type="button"
						on:click={() => {
							showUnarchiveAllConfirmDialog = true;
						}}
					>
						{#if loading}
							<Spinner className="size-3.5 shrink-0" />
						{:else}
							<UndoAction className="size-3.5 shrink-0" strokeWidth="1.5" />
						{/if}
						<div class="min-w-0 flex-1 truncate text-left">
							{$i18n.t('settings.personal.archivedChats.unarchiveAll.label')}
						</div>
					</button>

					<button
						class="flex h-[1.6875rem] w-full cursor-pointer select-none items-center gap-2 rounded-lg bg-transparent px-2 text-xs hover:text-gray-900 disabled:cursor-default disabled:opacity-30 dark:hover:text-gray-100"
						disabled={loading || chatCount === 0}
						type="button"
						on:click={exportChatsHandler}
					>
						<Download className="size-3.5 shrink-0" strokeWidth="1.5" />
						<div class="min-w-0 flex-1 truncate text-left">
							{$i18n.t('settings.personal.archivedChats.exportArchivedChats.label')}
						</div>
					</button>
				</DropdownMenu>
			</div>
		</Dropdown>
	</div>

	<div class="mt-3 flex-1 min-h-0 overflow-y-auto scrollbar-hover pr-1.5">
		{#if foldersEnabled}
			<div class="mb-4">
				<h3 class="mb-1 px-1 text-xs font-medium text-gray-700 dark:text-gray-300">
					{$i18n.t('Archived Folders')}
				</h3>
				{#if foldersLoadFailed}
					<button
						class="px-1 py-2 text-xs text-gray-500 hover:text-gray-900 dark:hover:text-white"
						type="button"
						on:click={loadFolders}
					>
						{$i18n.t('Failed to load archived folders. Click to retry.')}
					</button>
				{:else if archivedFolders === null}
					<div class="flex h-10 items-center justify-center">
						<Spinner className="size-4" />
					</div>
				{/if}
				{#if archivedFolders !== null}
					{#each filteredFolders as folder (folder.id)}
						<div class="flex w-full items-center gap-2 px-1 py-1 text-xs">
							<FolderIcon className="size-3.5 shrink-0 text-gray-400 dark:text-gray-600" />
							<span
								class="min-w-0 flex-1 truncate text-gray-600 dark:text-gray-400"
								title={folder.name}
							>
								{folder.name}
							</span>
							<Tooltip content={$i18n.t('Unarchive Folder')}>
								<button
									class="rounded-lg p-1 text-gray-400 transition-colors hover:text-gray-700 disabled:opacity-30 dark:text-gray-600 dark:hover:text-gray-300"
									type="button"
									aria-label={$i18n.t('Unarchive Folder')}
									disabled={restoringFolderId !== null}
									on:click={() => unarchiveFolderHandler(folder.id)}
								>
									{#if restoringFolderId === folder.id}
										<Spinner className="size-3.5" />
									{:else}
										<UndoAction className="size-3.5" strokeWidth="1.5" />
									{/if}
								</button>
							</Tooltip>
						</div>
					{:else}
						<div class="px-1 py-2 text-xs text-gray-500 dark:text-gray-400">
							{query ? $i18n.t('No results found') : $i18n.t('You have no archived folders.')}
						</div>
					{/each}
				{/if}
			</div>
			<h3 class="mb-1 px-1 text-xs font-medium text-gray-700 dark:text-gray-300">
				{$i18n.t('settings.personal.archivedChats.title')}
			</h3>
		{/if}
		{#if chatList === null}
			<div class="flex min-h-20 items-center justify-center">
				<Spinner className="size-5" />
			</div>
		{:else if chatList.length === 0}
			<div
				class="flex min-h-20 items-center justify-center px-4 text-center text-xs text-gray-500 dark:text-gray-400"
			>
				{$i18n.t('You have no archived conversations.')}
			</div>
		{:else}
			<div class="flex items-center px-1 text-xs text-gray-400 dark:text-gray-600">
				<button
					class="flex flex-1 items-center gap-1 py-0.5 text-left"
					type="button"
					on:click={() => setSortKey('title')}
				>
					{$i18n.t('Title')}
					{#if orderBy === 'title'}
						{#if direction === 'asc'}
							<ChevronUp className="size-2" />
						{:else}
							<ChevronDown className="size-2" />
						{/if}
					{/if}
				</button>
				<button
					class="hidden w-24 items-center justify-end gap-1 py-0.5 text-right sm:flex"
					type="button"
					on:click={() => setSortKey('updated_at')}
				>
					{$i18n.t('Updated at')}
					{#if orderBy === 'updated_at'}
						{#if direction === 'asc'}
							<ChevronUp className="size-2" />
						{:else}
							<ChevronDown className="size-2" />
						{/if}
					{/if}
				</button>
			</div>

			<div>
				{#each chatList as chat (chat.id)}
					<div class="flex w-full justify-between gap-2 px-1 py-1 text-xs">
						<a
							class="mr-auto min-w-0 max-w-xl flex-1 self-center truncate text-gray-600 hover:text-black dark:text-gray-400 dark:hover:text-white"
							href={`/c/${chat.id}`}
							title={chat?.title}
							on:click={(event) => {
								event.preventDefault();
								showSettings.set(false);
								goto(`/c/${chat.id}`);
							}}
						>
							{chat?.title}
						</a>
						<div
							class="hidden w-24 shrink-0 self-center justify-end text-gray-400 dark:text-gray-600 sm:flex"
						>
							{$i18n.t(
								dayjs(chat?.updated_at * 1000).calendar(null, {
									sameDay: '[Today]',
									nextDay: '[Tomorrow]',
									nextWeek: 'dddd',
									lastDay: '[Yesterday]',
									lastWeek: '[Last] dddd',
									sameElse: 'L'
								})
							)}
						</div>
						<div class="flex shrink-0 items-center justify-end text-gray-500 dark:text-gray-500">
							<Tooltip content={$i18n.t('Unarchive Chat')}>
								<button
									class="rounded-lg p-1 text-gray-400 transition-colors hover:text-gray-700 dark:text-gray-600 dark:hover:text-gray-300"
									type="button"
									aria-label={$i18n.t('Unarchive Chat')}
									on:click={() => {
										unarchiveHandler(chat.id);
									}}
								>
									<UndoAction className="size-3.5" strokeWidth="1.5" />
								</button>
							</Tooltip>
							{#if $user?.role === 'admin' || ($user?.permissions?.chat?.delete ?? true)}
								<Tooltip content={$i18n.t('Delete Chat')}>
									<button
										class="rounded-lg p-1 text-gray-400 transition-colors hover:text-gray-700 dark:text-gray-600 dark:hover:text-gray-300"
										type="button"
										aria-label={$i18n.t('Delete Chat')}
										on:click={() => {
											selectedChatId = chat.id;
											showDeleteConfirmDialog = true;
										}}
									>
										<Trash className="size-3.5" strokeWidth="1.5" />
									</button>
								</Tooltip>
							{/if}
						</div>
					</div>
				{/each}
			</div>

			{#if !allChatsLoaded}
				<Loader
					on:visible={() => {
						if (!chatListLoading) {
							loadMoreChats();
						}
					}}
				>
					<div class="flex w-full items-center justify-center gap-2 py-1 text-xs animate-pulse">
						<Spinner className="size-4" />
						<div>{$i18n.t('Loading...')}</div>
					</div>
				</Loader>
			{/if}
		{/if}
	</div>
</div>
