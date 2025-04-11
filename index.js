const fs = require("fs");
const readline = require("readline");
const translate = require("@vitalets/google-translate-api");

const inputFilePath = "input.py";   // 👈 Change this to your original file name
const outputFilePath = "output.py"; // 👈 This will be the translated file

async function translateFile() {
  const rl = readline.createInterface({
    input: fs.createReadStream(inputFilePath),
    crlfDelay: Infinity,
  });

  const outputLines = [];

  for await (const line of rl) {
    // Match string literals (both single and double quotes)
    const matches = line.match(/(["'])(?:(?=(\\?))\2.)*?\1/g);

    if (matches) {
      let translatedLine = line;

      for (const originalString of matches) {
        const unquoted = originalString.slice(1, -1);
        try {
          const res = await translate(unquoted, { from: "pt", to: "en" });

          // Replace only the specific string (preserving quotes)
          translatedLine = translatedLine.replace(originalString, `${originalString[0]}${res.text}${originalString[0]}`);
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
