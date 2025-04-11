const fs = require("fs");
const readline = require("readline");
const translate = require("google-translate-open-api").default;

const inputFilePath = "music.py";
const outputFilePath = "music_en.py";

async function translateFile() {
  const rl = readline.createInterface({
    input: fs.createReadStream(inputFilePath),
    crlfDelay: Infinity,
  });

  const outputLines = [];

  for await (const line of rl) {
    const matches = line.match(/(["'])(?:(?=(\\?))\2.)*?\1/g);

    if (matches) {
      let translatedLine = line;

      for (const originalString of matches) {
        const unquoted = originalString.slice(1, -1);

        try {
          const res = await translate(unquoted, {
            tld: "com",
            from: "pt",
            to: "en",
          });

          const translatedText = res.data[0];
          translatedLine = translatedLine.replace(originalString, `${originalString[0]}${translatedText}${originalString[0]}`);
        } catch (error) {
          console.error("Translation error:", error);
        }
      }

      outputLines.push(translatedLine);
    } else {
      outputLines.push(line);
    }
  }

  fs.writeFileSync(outputFilePath, outputLines.join("\n"), "utf8");
  console.log(`✅ Translation complete! File saved to: ${outputFilePath}`);
}

translateFile();
