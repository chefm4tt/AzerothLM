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

	db.lastScanTime = time()
	if not silent then
		print("|cFF00FF00AzerothLM|r: Data refresh complete.")
	end
end

-- ----------------------------------------------------------------------------
-- Signal Merge — imports topic data from AzerothLM_Signal into db.journal
-- ----------------------------------------------------------------------------
function AzerothLM_MergeSignal()
	local signal = _G["AzerothLM_Signal"]
	if not signal or type(signal) ~= "table" then return 0 end

	local db = _G[DB_NAME]
	if not db or not db.journal then return 0 end

	local newEntries = 0

	for slug, topicData in pairs(signal) do
		if type(topicData) == "table" and topicData.title then
			local existing = db.journal[slug]

			if not existing then
				-- Brand new topic: import with empty entries (updatedAt = 0 so all entries pass watermark)
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

	-- Deletion sync: remove journal topics missing from signal (only if signal has content)
	local signalHasContent = false
	for _ in pairs(signal) do signalHasContent = true; break end

	if signalHasContent then
		for slug, _ in pairs(db.journal) do
			if not signal[slug] then
				db.journal[slug] = nil
				if db.currentTopicSlug == slug then
					db.currentTopicSlug = nil
				end
			end
		end
	end

	-- Clear runtime global (file on disk persists; timestamp guard prevents re-import)
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
				currentTopicSlug = nil,
				lastScanTime = 0,
				journalVersion = 1,
			}
		end

		local db = _G[DB_NAME]

		-- Ensure new fields exist on existing DBs
		if not db.journal then db.journal = {} end
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

	else
		if not _G["AzerothLM_Frame"] then
			CreateAzerothLMFrame()
		end
		local frame = _G["AzerothLM_Frame"]
		frame:SetShown(not frame:IsShown())
	end
end
