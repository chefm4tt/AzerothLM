local addonName, ns = ...

-- ----------------------------------------------------------------------------
-- Constants & Globals
-- ----------------------------------------------------------------------------
local DB_NAME = "AzerothLM_DB"

-- ----------------------------------------------------------------------------
-- Core Logic
-- ----------------------------------------------------------------------------
local function StringToHex(str)
	if not str then return "" end
	return (string.gsub(tostring(str), ".", function (c)
		return string.format("%02X", string.byte(c))
	end))
end

function AzerothLM_UpdatePlayerContext(silent)
	if not _G[DB_NAME] then
		_G[DB_NAME] = {}
	end

	local db = _G[DB_NAME]
	if not silent then
		print("|cFF00FF00AzerothLM|r: Refreshing character data...")
	end

	-- 1. Equipped Gear (Slots 1-19)
	db.gear = {}
	for i = 1, 19 do
		local link = GetInventoryItemLink("player", i)
		table.insert(db.gear, StringToHex(link))
	end

	-- 2. Professions
	db.professions = {}
	local numSkills = GetNumSkillLines()
	for i = 1, numSkills do
		local skillName, isHeader, _, skillRank, _, _, skillMaxRank = GetSkillLineInfo(i)
		if not isHeader then
			table.insert(db.professions, {
				name = StringToHex(skillName),
				rank = skillRank,
				maxRank = skillMaxRank
			})
		end
	end

	-- 3. Active Quest Log
	db.quests = {}
	local numEntries, numQuests = GetNumQuestLogEntries()
	for i = 1, numEntries do
		local title, level, _, isHeader, _, isComplete, _, questID = GetQuestLogTitle(i)
		if not isHeader then
			table.insert(db.quests, {
				id = questID,
				title = StringToHex(title),
				level = level,
				isComplete = isComplete
			})
		end
	end

	-- 4. Character Basics
	db.level = UnitLevel("player")
	db.class = select(2, UnitClass("player"))
	db.race = select(2, UnitRace("player"))
	db.zone = GetZoneText()
	db.subzone = GetSubZoneText()
	db.gold = math.floor(GetMoney() / 10000)

	-- 5. Talent Spec (tree names + points spent)
	db.talents = {}
	for tab = 1, 3 do
		local name, _, pointsSpent = GetTalentTabInfo(tab)
		if name then
			db.talents[tab] = { name = StringToHex(name), spent = pointsSpent }
		end
	end

	-- 6. Key Reputations (non-Exalted factions only)
	db.reputations = {}
	for i = 1, GetNumFactions() do
		local name, _, standingId, _, _, _, _, _, isHeader = GetFactionInfo(i)
		if not isHeader and standingId and standingId < 8 then
			table.insert(db.reputations, {
				name = StringToHex(name),
				standing = standingId
			})
		end
	end

	db.lastScanTime = time()
	if not silent then
		print("|cFF00FF00AzerothLM|r: Data refresh complete.")
	end
end

-- ----------------------------------------------------------------------------
-- Pending Actions — queue in-game management actions for relay sync
-- ----------------------------------------------------------------------------
function ns.ApplyActionLocally(action)
	local db = _G[DB_NAME]
	if not db or not db.journal then return end

	if action.action == "delete_topic" then
		db.journal[action.slug] = nil
		if db.currentTopicSlug == action.slug then
			db.currentTopicSlug = nil
		end

	elseif action.action == "clear_entries" then
		local topic = db.journal[action.slug]
		if topic then
			topic.entries = {}
			topic.updatedAt = action.timestamp
		end

	elseif action.action == "rename_topic" then
		local topic = db.journal[action.slug]
		if topic then
			topic.title = action.newTitle
		end

	elseif action.action == "delete_entry" then
		local topic = db.journal[action.slug]
		if topic and topic.entries then
			for i = #topic.entries, 1, -1 do
				if topic.entries[i].timestamp == action.entryTimestamp then
					table.remove(topic.entries, i)
					break
				end
			end
		end
	end
end

function ns.QueueAction(actionData)
	local db = _G[DB_NAME]
	if not db then return end
	if not db.pendingActions then db.pendingActions = {} end

	actionData.timestamp = time()
	table.insert(db.pendingActions, actionData)

	ns.ApplyActionLocally(actionData)

	if AzerothLM_UpdateJournalDisplay then
		AzerothLM_UpdateJournalDisplay()
	end

	print("|cFF00FF00AzerothLM|r: Action queued. Click Refresh to sync.")
end

-- ----------------------------------------------------------------------------
-- Signal Merge — imports topic data from AzerothLM_Signal into db.journal
-- ----------------------------------------------------------------------------
function AzerothLM_MergeSignal()
	local signal = _G["AzerothLM_Signal"]
	if not signal or type(signal) ~= "table" then return 0 end

	local db = _G[DB_NAME]
	if not db or not db.journal then return 0 end

	-- Process relay acknowledgment of pending actions
	local ack = signal["_ack"]
	if ack and ack.processedUpTo and db.pendingActions then
		local remaining = {}
		for _, action in ipairs(db.pendingActions) do
			if action.timestamp > ack.processedUpTo then
				table.insert(remaining, action)
			end
		end
		db.pendingActions = remaining
	end

	-- Build set of slugs with unacknowledged pending deletions (don't re-import these)
	local pendingDeletedSlugs = {}
	if db.pendingActions then
		for _, action in ipairs(db.pendingActions) do
			if action.action == "delete_topic" then
				pendingDeletedSlugs[action.slug] = true
			end
		end
	end

	local newEntries = 0

	for slug, topicData in pairs(signal) do
		if slug ~= "_ack" and not pendingDeletedSlugs[slug]
		   and type(topicData) == "table" and topicData.title then
			local existing = db.journal[slug]

			if not existing then
				db.journal[slug] = {
					title = topicData.title,
					createdAt = topicData.createdAt or 0,
					updatedAt = 0,
					model = topicData.model or "unknown",
					entries = {},
				}
				existing = db.journal[slug]
			end

			-- Watermark: use last existing entry's timestamp (NOT updatedAt)
			local lastTimestamp = 0
			if existing.entries and #existing.entries > 0 then
				lastTimestamp = existing.entries[#existing.entries].timestamp or 0
			end

			-- Merge entries newer than watermark
			if topicData.entries and type(topicData.entries) == "table" then
				for _, entry in ipairs(topicData.entries) do
					if type(entry) == "table" and entry.timestamp and entry.timestamp > lastTimestamp then
						table.insert(existing.entries, {
							question = entry.question or "",
							answer = entry.answer or "",
							timestamp = entry.timestamp,
						})
						newEntries = newEntries + 1
					end
				end
			end

			-- Update metadata
			if topicData.updatedAt and topicData.updatedAt > (existing.updatedAt or 0) then
				existing.updatedAt = topicData.updatedAt
			end
			if topicData.model then
				existing.model = topicData.model
			end
		end
	end

	-- Deletion sync: remove journal topics missing from signal
	for slug, _ in pairs(db.journal) do
		if not signal[slug] and not pendingDeletedSlugs[slug] then
			db.journal[slug] = nil
			if db.currentTopicSlug == slug then
				db.currentTopicSlug = nil
			end
		end
	end

	-- Re-apply remaining pending actions (rename, clear, delete_entry may conflict with signal data)
	if db.pendingActions and #db.pendingActions > 0 then
		for _, action in ipairs(db.pendingActions) do
			ns.ApplyActionLocally(action)
		end
	end

	-- Cache signal for /alm reset, then clear runtime global
	ns.lastSignal = signal
	_G["AzerothLM_Signal"] = nil

	return newEntries
end

-- ----------------------------------------------------------------------------
-- Legacy Migration — converts old chat-based DB to journal format (one-time)
-- ----------------------------------------------------------------------------
local function MigrateLegacyChats(db)
	if not db.chats or #db.chats == 0 then return end
	if db.journalVersion and db.journalVersion >= 1 then return end

	if not db.journal then db.journal = {} end

	for i, chat in ipairs(db.chats) do
		local chatName = chat.name or ("Chat " .. i)
		local slug = "legacy-" .. i

		local entries = {}
		local messages = chat.messages or {}

		-- Pair consecutive You/AI messages into Q&A entries
		local j = 1
		while j <= #messages do
			local msg = messages[j]
			if msg.sender == "You" then
				local question = msg.text or ""
				local answer = ""
				if j + 1 <= #messages and messages[j + 1].sender == "AI" then
					answer = messages[j + 1].text or ""
					j = j + 1
				end
				table.insert(entries, {
					question = question,
					answer = answer,
					timestamp = time(),
				})
			end
			j = j + 1
		end

		if #entries > 0 then
			db.journal[slug] = {
				title = chatName,
				createdAt = time(),
				updatedAt = time(),
				model = "migrated",
				entries = entries,
			}
		end
	end

	-- Clean up legacy fields
	db.chats = nil
	db.currentChatID = nil
	db.status = nil
	db.query = nil
	db.response = nil
	db.lastSyncTime = nil
	db.journalVersion = 1
end

-- ----------------------------------------------------------------------------
-- Event Handling
-- ----------------------------------------------------------------------------
local f = CreateFrame("Frame")
f:RegisterEvent("ADDON_LOADED")
f:SetScript("OnEvent", function(self, event, ...)
	local name = ...
	if event == "ADDON_LOADED" and name == "AzerothLM" then
		if self.processed then return end
		self.processed = true

		-- Initialize DB with new schema
		if not _G[DB_NAME] then
			_G[DB_NAME] = {
				gear = {},
				professions = {},
				quests = {},
				journal = {},
				pendingActions = {},
				currentTopicSlug = nil,
				lastScanTime = 0,
				journalVersion = 1,
			}
		end

		local db = _G[DB_NAME]

		-- Ensure new fields exist on existing DBs
		if not db.journal then db.journal = {} end
		if not db.pendingActions then db.pendingActions = {} end
		if not db.journalVersion then db.journalVersion = 0 end
		if not db.lastScanTime then db.lastScanTime = 0 end

		-- Migrate legacy chats if present
		MigrateLegacyChats(db)

		-- Merge signal data
		local newEntries = AzerothLM_MergeSignal()

		-- Auto-scan character context
		AzerothLM_UpdatePlayerContext(true)

		-- Update display if frame exists
		if AzerothLM_UpdateJournalDisplay then
			AzerothLM_UpdateJournalDisplay()
		end

		if newEntries > 0 then
			print(string.format("|cFF00FF00AzerothLM|r: Loaded %d new journal entries.", newEntries))
		else
			print("|cFF00FF00AzerothLM|r: Research Journal ready.")
		end

		self:UnregisterEvent("ADDON_LOADED")
	end
end)

-- ----------------------------------------------------------------------------
-- Slash Command
-- ----------------------------------------------------------------------------
SLASH_AZEROTHLM1 = "/alm"
SlashCmdList["AZEROTHLM"] = function(msg)
	msg = strtrim(msg or "")

	if msg == "scan" then
		AzerothLM_UpdatePlayerContext()

	elseif msg == "refresh" then
		print("|cFF00FF00AzerothLM|r: Reloading to pull latest journal data...")
		ReloadUI()

	elseif msg == "topics" then
		local db = _G[DB_NAME]
		if db and db.journal then
			local count = 0
			for slug, topic in pairs(db.journal) do
				count = count + 1
				local entryCount = topic.entries and #topic.entries or 0
				print(string.format("|cFF00FF00AzerothLM|r: [%s] %s (%d entries)", slug, topic.title, entryCount))
			end
			if count == 0 then
				print("|cFF00FF00AzerothLM|r: No topics yet. Use MCP tools to create one.")
			end
		end

	elseif string.match(msg, "^delentry%s+(%d+)$") then
		local idx = tonumber(string.match(msg, "^delentry%s+(%d+)$"))
		local db = _G[DB_NAME]
		if not db or not db.currentTopicSlug then
			print("|cFF00FF00AzerothLM|r: No topic selected.")
			return
		end
		local topic = db.journal and db.journal[db.currentTopicSlug]
		if not topic or not topic.entries or idx < 1 or idx > #topic.entries then
			print("|cFF00FF00AzerothLM|r: Invalid entry index.")
			return
		end
		ns.QueueAction({
			action = "delete_entry",
			slug = db.currentTopicSlug,
			entryTimestamp = topic.entries[idx].timestamp,
		})

	elseif msg == "reset" then
		local db = _G[DB_NAME]
		if not db then return end
		wipe(db.journal)
		db.pendingActions = {}
		db.currentTopicSlug = nil
		-- Re-merge from cached signal (if available)
		if ns.lastSignal then
			_G["AzerothLM_Signal"] = ns.lastSignal
			local restored = AzerothLM_MergeSignal()
			print(string.format("|cFF00FF00AzerothLM|r: Reset complete. %d entries restored.", restored))
		else
			print("|cFF00FF00AzerothLM|r: Reset complete. Type |cFFFFFF00/alm refresh|r to re-sync with relay.")
		end
		if AzerothLM_UpdateJournalDisplay then
			AzerothLM_UpdateJournalDisplay()
		end

	elseif msg == "help" then
		print("|cFF00FF00AzerothLM Commands:|r")
		print("  |cFFFFFF00/alm|r — Toggle journal window")
		print("  |cFFFFFF00/alm scan|r — Refresh character context")
		print("  |cFFFFFF00/alm refresh|r — Reload UI to sync with relay")
		print("  |cFFFFFF00/alm topics|r — List topics in chat")
		print("  |cFFFFFF00/alm delentry N|r — Delete entry N from current topic")
		print("  |cFFFFFF00/alm reset|r — Clear and rebuild journal from last sync")
		print("  |cFFFFFF00/alm wipe|r — Wipe all journal data")
		print("  |cFFFFFF00/alm help|r — Show this help")

	elseif msg == "wipe" then
		local db = _G[DB_NAME]
		if not db then return end
		-- Queue relay-side deletions before wiping
		local deleteActions = {}
		for slug, _ in pairs(db.journal) do
			table.insert(deleteActions, {
				action = "delete_topic",
				slug = slug,
				timestamp = time(),
			})
		end
		wipe(db.journal)
		db.pendingActions = deleteActions
		db.currentTopicSlug = nil
		ns.lastSignal = nil
		if AzerothLM_UpdateJournalDisplay then
			AzerothLM_UpdateJournalDisplay()
		end
		local count = #deleteActions
		if count > 0 then
			print(string.format("|cFF00FF00AzerothLM|r: Wiped %d topics. Deletions queued for relay sync.", count))
		else
			print("|cFF00FF00AzerothLM|r: Journal already empty.")
		end

	else
		if not _G["AzerothLM_Frame"] then
			CreateAzerothLMFrame()
		end
		local frame = _G["AzerothLM_Frame"]
		frame:SetShown(not frame:IsShown())
	end
end
