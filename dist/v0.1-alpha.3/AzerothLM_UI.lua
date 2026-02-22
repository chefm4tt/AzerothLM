local addonName, ns = ...
local DB_NAME = "AzerothLM_DB"

-- ----------------------------------------------------------------------------
-- Utilities
-- ----------------------------------------------------------------------------
local function HexToText(hex)
	if not hex then return "" end
	if #hex > 0 and #hex % 2 == 0 and string.match(hex, "^%x+$") then
		return (string.gsub(hex, "..", function (cc)
			return string.char(tonumber(cc, 16))
		end))
	end
	return hex
end

-- ----------------------------------------------------------------------------
-- Context Menu & Popup Dialogs
-- ----------------------------------------------------------------------------
local contextMenuFrame = CreateFrame("Frame", "AzerothLM_ContextMenu", UIParent, "UIDropDownMenuTemplate")

StaticPopupDialogs["AZEROTHLM_DELETE_TOPIC"] = {
	text = "Delete topic \"%s\"?\n\nThis will remove the topic and all its entries.\nClick Refresh to sync the change.",
	button1 = "Delete",
	button2 = "Cancel",
	OnAccept = function(self, slug)
		ns.QueueAction({ action = "delete_topic", slug = slug })
	end,
	timeout = 0,
	whileDead = true,
	hideOnEscape = true,
}

StaticPopupDialogs["AZEROTHLM_CLEAR_ENTRIES"] = {
	text = "Clear all entries from \"%s\"?\n\nThe topic will remain but all Q&A will be removed.",
	button1 = "Clear",
	button2 = "Cancel",
	OnAccept = function(self, slug)
		ns.QueueAction({ action = "clear_entries", slug = slug })
	end,
	timeout = 0,
	whileDead = true,
	hideOnEscape = true,
}

StaticPopupDialogs["AZEROTHLM_RENAME_TOPIC"] = {
	text = "Rename topic:",
	button1 = ACCEPT,
	button2 = CANCEL,
	hasEditBox = true,
	editBoxWidth = 200,
	maxLetters = 64,
	OnShow = function(self, data)
		self.editBox:SetText(data.currentTitle or "")
		self.editBox:HighlightText()
		self.editBox:SetFocus()
	end,
	OnAccept = function(self, data)
		local newTitle = strtrim(self.editBox:GetText())
		if newTitle ~= "" then
			ns.QueueAction({ action = "rename_topic", slug = data.slug, newTitle = newTitle })
		end
	end,
	EditBoxOnEnterPressed = function(self)
		self:GetParent().button1:Click()
	end,
	EditBoxOnEscapePressed = function(self)
		self:GetParent():Hide()
	end,
	timeout = 0,
	whileDead = true,
	hideOnEscape = true,
}

StaticPopupDialogs["AZEROTHLM_DELETE_ENTRY"] = {
	text = "Delete entry Q%d from \"%s\"?",
	button1 = "Delete",
	button2 = "Cancel",
	OnAccept = function(self, data)
		ns.QueueAction({ action = "delete_entry", slug = data.slug, entryTimestamp = data.entryTimestamp })
	end,
	timeout = 0,
	whileDead = true,
	hideOnEscape = true,
}

-- ----------------------------------------------------------------------------
-- Response Formatting
-- ----------------------------------------------------------------------------
local QUALITY_COLORS = {
	[0] = "ff9d9d9d", -- Poor (gray)
	[1] = "ffffffff", -- Common (white)
	[2] = "ff1eff00", -- Uncommon (green)
	[3] = "ff0070dd", -- Rare (blue)
	[4] = "ffa335ee", -- Epic (purple)
	[5] = "ffff8000", -- Legendary (orange)
}

local function FormatResponseLine(text)
	-- 1. Clean markdown artifacts
	text = text:gsub("%*%*([^%*]+)%*%*", "%1")   -- Strip **bold**
	text = text:gsub("%*([^%*]+)%*", "%1")         -- Strip *italic*

	-- 2. Replace bullet markers with Unicode bullet
	text = text:gsub("^(%s*)[%*%-]%s", "%1\226\128\162 ")  -- UTF-8 for •

	-- 3. Colorize items with explicit ID: [Item Name] (ID:12345) or (ID 12345)
	text = text:gsub("%[([^%]]+)%]%s*%(ID[:%s](%d+)%)", function(name, id)
		local _, _, quality = GetItemInfo(tonumber(id))
		if quality and QUALITY_COLORS[quality] then
			return "|c" .. QUALITY_COLORS[quality] .. "[" .. name .. "]|r"
		end
		return "[" .. name .. "]"
	end)

	-- 4. Try name-based lookup for remaining [Item Name] without IDs
	text = text:gsub("%[([^%]]+)%]", function(name)
		local _, _, quality = GetItemInfo(name)
		if quality and QUALITY_COLORS[quality] then
			return "|c" .. QUALITY_COLORS[quality] .. "[" .. name .. "]|r"
		end
		return "[" .. name .. "]"
	end)

	return text
end

-- ----------------------------------------------------------------------------
-- Journal Display
-- ----------------------------------------------------------------------------
function AzerothLM_UpdateJournalDisplay()
	local f = _G["AzerothLM_Frame"]
	if not f then return end

	local db = _G[DB_NAME]
	if not db or not db.journal then return end

	-- Update sidebar topic list
	if f.RefreshTopics then
		f:RefreshTopics()
	end

	-- Update Refresh button to show pending action count
	if f.refreshBtn then
		local pendingCount = 0
		if db.pendingActions then
			for _, action in ipairs(db.pendingActions) do
				if not (action.action == "delete_topic" and not db.journal[action.slug]) then
					pendingCount = pendingCount + 1
				end
			end
		end
		if pendingCount > 0 then
			f.refreshBtn:SetText(string.format("Sync (%d)", pendingCount))
		else
			f.refreshBtn:SetText("Refresh")
		end
	end

	-- Clear the main content area
	f.history:Clear()

	local slug = db.currentTopicSlug
	if not slug or not db.journal[slug] then
		f.history:AddMessage("|cFF888888No topic selected. Use the sidebar to choose a topic.|r")
		if f.topicHeader then f.topicHeader:SetText("") end
		if f.topicMeta then f.topicMeta:SetText("") end
		return
	end

	local topic = db.journal[slug]

	-- Update header
	if f.topicHeader then
		f.topicHeader:SetText("|cFFFFD100" .. (topic.title or slug) .. "|r")
	end
	if f.topicMeta then
		local entryCount = topic.entries and #topic.entries or 0
		f.topicMeta:SetText(string.format(
			"|cFF888888Model: %s | Entries: %d|r",
			topic.model or "unknown",
			entryCount
		))
	end

	-- Render entries
	if not topic.entries or #topic.entries == 0 then
		f.history:AddMessage("|cFF888888No entries yet. Use MCP tools to ask a question on this topic.|r")
		return
	end

	for i, entry in ipairs(topic.entries) do
		-- Question (cyan)
		local qText = entry.question or ""
		local qLines = { strsplit("\n", qText) }
		local qHeaderPrinted = false
		for _, line in ipairs(qLines) do
			if line and line ~= "" then
				if not qHeaderPrinted then
					f.history:AddMessage(string.format("|cFF00FFFFQ%d:|r %s", i, line))
					qHeaderPrinted = true
				else
					f.history:AddMessage("    " .. line)
				end
			end
		end

		-- Answer (green header, formatted content)
		local aText = entry.answer or ""
		local aLines = { strsplit("\n", aText) }
		local aHeaderPrinted = false
		for _, line in ipairs(aLines) do
			if line and line ~= "" then
				local formatted = FormatResponseLine(line)
				if not aHeaderPrinted then
					f.history:AddMessage("|cFF00FF00A:|r " .. formatted)
					aHeaderPrinted = true
				else
					f.history:AddMessage(formatted)
				end
			end
		end

		-- Separator between entries
		if i < #topic.entries then
			f.history:AddMessage(" ")
		end
	end
end

-- ----------------------------------------------------------------------------
-- Frame Creation
-- ----------------------------------------------------------------------------
function CreateAzerothLMFrame()
	local f = CreateFrame("Frame", "AzerothLM_Frame", UIParent, "BackdropTemplate")
	f:SetSize(650, 450)
	f:SetPoint("CENTER")
	f:SetBackdrop({
		bgFile = "Interface\\DialogFrame\\UI-DialogBox-Background",
		edgeFile = "Interface\\DialogFrame\\UI-DialogBox-Border",
		tile = true, tileSize = 32, edgeSize = 32,
		insets = { left = 11, right = 12, top = 12, bottom = 11 }
	})
	f:EnableMouse(true)
	f:SetMovable(true)
	f:RegisterForDrag("LeftButton")
	f:SetScript("OnDragStart", f.StartMoving)
	f:SetScript("OnDragStop", f.StopMovingOrSizing)

	-- Title
	f.title = f:CreateFontString(nil, "OVERLAY", "GameFontNormal")
	f.title:SetPoint("TOP", 0, -12)
	f.title:SetText("AzerothLM Research Journal")

	-- Close Button
	f.closeBtn = CreateFrame("Button", nil, f, "UIPanelCloseButton")
	f.closeBtn:SetPoint("TOPRIGHT", -2, -2)

	-- Refresh Button
	f.refreshBtn = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
	f.refreshBtn:SetSize(60, 20)
	f.refreshBtn:SetPoint("RIGHT", f.closeBtn, "LEFT", -5, 0)
	f.refreshBtn:SetText("Refresh")
	f.refreshBtn:SetScript("OnClick", function()
		ReloadUI()
	end)

	-- Sidebar
	f.sidebar = CreateFrame("Frame", nil, f, "BackdropTemplate")
	f.sidebar:SetPoint("TOPLEFT", 12, -30)
	f.sidebar:SetPoint("BOTTOMLEFT", 12, 12)
	f.sidebar:SetWidth(140)
	f.sidebar:SetBackdrop({
		bgFile = "Interface\\DialogFrame\\UI-DialogBox-Background",
		edgeFile = "Interface\\Tooltips\\UI-Tooltip-Border",
		tile = true, tileSize = 16, edgeSize = 16,
		insets = { left = 4, right = 4, top = 4, bottom = 4 }
	})

	-- Sidebar title
	f.sidebarTitle = f.sidebar:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
	f.sidebarTitle:SetPoint("TOP", 0, -8)
	f.sidebarTitle:SetText("|cFFFFD100Topics|r")

	-- Topic header area (above scrolling content)
	f.topicHeader = f:CreateFontString(nil, "OVERLAY", "GameFontNormalLarge")
	f.topicHeader:SetPoint("TOPLEFT", f.sidebar, "TOPRIGHT", 10, -2)
	f.topicHeader:SetPoint("RIGHT", f.refreshBtn, "LEFT", -10, 0)
	f.topicHeader:SetJustifyH("LEFT")
	f.topicHeader:SetText("")

	f.topicMeta = f:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
	f.topicMeta:SetPoint("TOPLEFT", f.topicHeader, "BOTTOMLEFT", 0, -2)
	f.topicMeta:SetJustifyH("LEFT")
	f.topicMeta:SetText("")

	-- Scrolling Message Frame (main content)
	f.history = CreateFrame("ScrollingMessageFrame", "AzerothLM_Journal", f)
	f.history:SetPoint("TOPLEFT", f.sidebar, "TOPRIGHT", 10, -40)
	f.history:SetPoint("BOTTOMRIGHT", -20, 16)
	f.history:SetFontObject("ChatFontNormal")
	f.history:SetJustifyH("LEFT")
	f.history:SetFading(false)
	f.history:SetMaxLines(1000)
	f.history:SetInsertMode("BOTTOM")
	f.history:EnableMouseWheel(true)
	f.history:SetScript("OnMouseWheel", function(self, delta)
		if delta > 0 then
			self:ScrollUp()
		else
			self:ScrollDown()
		end
	end)
	f.history:AddMessage("Welcome to AzerothLM Research Journal.")

	-- Topic sidebar buttons
	f.topicButtons = {}

	function f:RefreshTopics()
		local db = _G[DB_NAME]
		if not db or not db.journal then return end

		-- Hide existing buttons
		for _, btn in pairs(self.topicButtons) do btn:Hide() end

		-- Build sorted topic list (by updatedAt descending)
		local sortedTopics = {}
		for slug, topic in pairs(db.journal) do
			table.insert(sortedTopics, {
				slug = slug,
				title = topic.title or slug,
				updatedAt = topic.updatedAt or 0,
				entryCount = topic.entries and #topic.entries or 0,
			})
		end
		table.sort(sortedTopics, function(a, b) return a.updatedAt > b.updatedAt end)

		local yOffset = -24
		for i, info in ipairs(sortedTopics) do
			local btn = self.topicButtons[i]
			if not btn then
				btn = CreateFrame("Button", nil, self.sidebar)
				btn:SetSize(125, 20)
				btn:SetNormalFontObject("GameFontNormalSmall")
				btn:SetHighlightFontObject("GameFontHighlightSmall")
				local highlight = btn:CreateTexture(nil, "HIGHLIGHT")
				highlight:SetAllPoints()
				highlight:SetColorTexture(1, 1, 1, 0.15)
				btn:SetHighlightTexture(highlight)
				self.topicButtons[i] = btn
			end

			btn:SetPoint("TOP", self.sidebar, "TOP", 0, yOffset)

			-- Truncate title to fit button
			local displayTitle = info.title
			if #displayTitle > 16 then
				displayTitle = string.sub(displayTitle, 1, 14) .. ".."
			end
			btn:SetText(displayTitle)

			-- Highlight active topic
			if db.currentTopicSlug == info.slug then
				btn:LockHighlight()
			else
				btn:UnlockHighlight()
			end

			-- Click handler (left = select, right = context menu)
			local capturedSlug = info.slug
			local capturedTitle = info.title
			btn:RegisterForClicks("LeftButtonUp", "RightButtonUp")
			btn:SetScript("OnClick", function(self, button)
				if button == "RightButton" then
					local menuList = {
						{ text = capturedTitle, isTitle = true, notCheckable = true },
						{
							text = "Rename Topic",
							notCheckable = true,
							func = function()
								local dialog = StaticPopup_Show("AZEROTHLM_RENAME_TOPIC")
								if dialog then
									dialog.data = { slug = capturedSlug, currentTitle = capturedTitle }
								end
							end,
						},
						{
							text = "Clear All Entries",
							notCheckable = true,
							func = function()
								local dialog = StaticPopup_Show("AZEROTHLM_CLEAR_ENTRIES", capturedTitle)
								if dialog then
									dialog.data = capturedSlug
								end
							end,
						},
						{
							text = "Delete Last Entry",
							notCheckable = true,
							func = function()
								local curDb = _G[DB_NAME]
								local topic = curDb and curDb.journal and curDb.journal[capturedSlug]
								if topic and topic.entries and #topic.entries > 0 then
									local lastEntry = topic.entries[#topic.entries]
									local dialog = StaticPopup_Show("AZEROTHLM_DELETE_ENTRY", #topic.entries, capturedTitle)
									if dialog then
										dialog.data = { slug = capturedSlug, entryTimestamp = lastEntry.timestamp }
									end
								else
									print("|cFF00FF00AzerothLM|r: No entries to delete.")
								end
							end,
						},
						{
							text = "|cFFFF4444Delete Topic|r",
							notCheckable = true,
							func = function()
								local dialog = StaticPopup_Show("AZEROTHLM_DELETE_TOPIC", capturedTitle)
								if dialog then
									dialog.data = capturedSlug
								end
							end,
						},
						{ text = "Cancel", notCheckable = true },
					}
					EasyMenu(menuList, contextMenuFrame, "cursor", 0, 0, "MENU")
				else
					db.currentTopicSlug = capturedSlug
					AzerothLM_UpdateJournalDisplay()
				end
			end)

			-- Tooltip for full title + entry count
			btn:SetScript("OnEnter", function(self)
				GameTooltip:SetOwner(self, "ANCHOR_RIGHT")
				GameTooltip:SetText(capturedTitle, 1, 1, 1)
				GameTooltip:AddLine(string.format("%d entries | Right-click for options", info.entryCount), 0.7, 0.7, 0.7)
				GameTooltip:Show()
			end)
			btn:SetScript("OnLeave", function()
				GameTooltip:Hide()
			end)

			btn:Show()
			yOffset = yOffset - 22
		end

		-- "No topics" message
		if #sortedTopics == 0 then
			if not self.noTopicsLabel then
				self.noTopicsLabel = self.sidebar:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
				self.noTopicsLabel:SetPoint("CENTER", 0, 0)
				self.noTopicsLabel:SetText("|cFF888888No topics yet|r")
			end
			self.noTopicsLabel:Show()
		elseif self.noTopicsLabel then
			self.noTopicsLabel:Hide()
		end
	end

	-- Auto-select first topic on show
	f:SetScript("OnShow", function()
		local db = _G[DB_NAME]
		if db and db.journal and not db.currentTopicSlug then
			local bestSlug, bestTime = nil, 0
			for slug, topic in pairs(db.journal) do
				if (topic.updatedAt or 0) > bestTime then
					bestSlug = slug
					bestTime = topic.updatedAt or 0
				end
			end
			if bestSlug then
				db.currentTopicSlug = bestSlug
			end
		end
		AzerothLM_UpdateJournalDisplay()
	end)

	f:Hide()
end
