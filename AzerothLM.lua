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

-- ----------------------------------------------------------------------------
-- Event Handling
-- ----------------------------------------------------------------------------
local f = CreateFrame("Frame")
f:RegisterEvent("ADDON_LOADED")
f:SetScript("OnEvent", function(self, event, ...)
	local name = ...
	if event == "ADDON_LOADED" and name == "AzerothLM" then
		if not _G[DB_NAME] then
			_G[DB_NAME] = {
				status = "IDLE",
				gear = {},
				professions = {},
				quests = {},
				chats = {},
				currentChatID = 1,
			}
		end
		
		local db = _G[DB_NAME]

		if not db.chats then db.chats = {} end
		if not db.currentChatID then db.currentChatID = 1 end

		if #db.chats == 0 then
			table.insert(db.chats, { name = "General", messages = {} })
		end

		if db.status == "COMPLETE" then
			local chat = db.chats[db.currentChatID]
			if chat and db.response then
				table.insert(chat.messages, { sender = "AI", text = db.response })
			end
			db.response = nil
			db.status = "IDLE"
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