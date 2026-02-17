local addonName, ns = ...

-- ----------------------------------------------------------------------------
-- Constants & Globals
-- ----------------------------------------------------------------------------
local DB_NAME = "AzerothLM_DB"
AzerothLM_Response = ""

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
	-- Ensure DB exists
	if not _G[DB_NAME] then
		_G[DB_NAME] = { status = "IDLE" }
	end
	
	local db = _G[DB_NAME]
	db.status = "SCANNING"
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

	db.status = "IDLE"
	if not silent then
		print("|cFF00FF00AzerothLM|r: Data refresh complete.")
	end
end

function AzerothLM_ManualPull()
	local signal = _G["AzerothLM_Signal"]
	if signal and type(signal) == "table" and signal.response and signal.response ~= "" then
		local db = _G[DB_NAME]
		if db and db.chats and signal.chatID then
			local chat = db.chats[signal.chatID]
			if chat then
				table.insert(chat.messages, { sender = "AI", text = signal.response })
			end
		end
		if db then
			db.status = "IDLE"
			db.query = ""
		end
		_G["AzerothLM_Signal"] = nil
		if AzerothLM_UpdateTerminalDisplay then AzerothLM_UpdateTerminalDisplay() end
		print('|cff00ff00[AzerothLM]|r Message successfully pulled!')
	end
end

function AzerothLM_ForceReset()
	local db = _G[DB_NAME]
	if db then
		db.status = "IDLE"
		db.query = ""
		db.lastSyncTime = 0
		if AzerothLM_UpdateTerminalDisplay then
			AzerothLM_UpdateTerminalDisplay()
		end
		print("|cFF00FF00AzerothLM|r: Manual reset performed.")
	end
end

-- ----------------------------------------------------------------------------
-- Event Handling
-- ----------------------------------------------------------------------------
local f = CreateFrame("Frame")
f:RegisterEvent("ADDON_LOADED")
f:SetScript("OnEvent", function(self, event, ...)
	local name = ...
	if event == "ADDON_LOADED" and name == "AzerothLM" then
		print('[AzerothLM Debug] Signal Value: ' .. tostring(AzerothLM_Response))

		AzerothLM_ManualPull()

		-- Race Condition Protection
		if self.processed then return end
		self.processed = true

		if not _G[DB_NAME] then
			_G[DB_NAME] = {
				status = "IDLE",
				lastSyncTime = 0,
				gear = {},
				professions = {},
				quests = {},
				chats = {},
				currentChatID = 1,
			}
		end
		
		local db = _G[DB_NAME]
		if not db.lastSyncTime then db.lastSyncTime = 0 end

		if not db.chats then db.chats = {} end
		if not db.currentChatID then db.currentChatID = 1 end

		if #db.chats == 0 then
			table.insert(db.chats, { name = "General", messages = {} })
		end

		if AzerothLM_UpdateTerminalDisplay then
			AzerothLM_UpdateTerminalDisplay()
		end

		self:UnregisterEvent("ADDON_LOADED")
	end
end)

-- ----------------------------------------------------------------------------
-- Slash Command
-- ----------------------------------------------------------------------------
SLASH_AZEROTHLM1 = "/alm"
SlashCmdList["AZEROTHLM"] = function(msg)
	if msg == "scan" then
		AzerothLM_UpdatePlayerContext()
	else
		if not _G["AzerothLM_Frame"] then
			CreateAzerothLMFrame()
		end
		local f = _G["AzerothLM_Frame"]
		f:SetShown(not f:IsShown())
	end
end