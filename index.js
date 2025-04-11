const fs = require("fs");
const readline = require("readline");
const fetch = require("node-fetch");

const inputFilePath = "music.py";
const outputFilePath = "music_en.py";

async function translate(text) {
  const res = await fetch("https://libretranslate.de/translate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      q: text,
      source: "pt",
      target: "en",
      format: "text"
    }),
  });

  const data = await res.json();
  return data.translatedText;
}

async function translateFile() {
  const rl = readline.createInterface({
    input: fs.createReadStream(inputFilePath),
    crlfDelay: Infinity,
  });

  const outputLines = [];

  for await (const line of rl) {
    const matches = line.match(/(["'])(?:(?=(\\?))\2.)*?\1/g);
    let translatedLine = line;

    if (matches) {
      for (const originalString of matches) {
        const unquoted = originalString.slice(1, -1);
        try {
          const translated = await translate(unquoted);
          translatedLine = translatedLine.replace(originalString, `${originalString[0]}${translated}${originalString[0]}`);
        } catch (e) {
          console.error("Translate error:", e.message);
        }
      }
    }

    outputLines.push(translatedLine);
  }

  fs.writeFileSync(outputFilePath, outputLines.join("\n"), "utf8");
  console.log("✅ Done! Translated file saved:", outputFilePath);
}

translateFile();
