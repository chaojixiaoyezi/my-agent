/**
 * 多行输入（2026-08-17 完整版收尾：hermes 同款多行编辑）。
 * Enter 提交；Ctrl+J 换行；字符/退格直通；上下箭头与 Tab 交给 App
 * （输入历史 / 斜杠补全）。
 */
import React from "react";
import { Text, useInput } from "ink";
import type { Theme } from "../theme.js";

export interface MultiLineInputProps {
  value: string;
  onChange: (value: string) => void;
  onSubmit: (value: string) => void;
  theme: Theme;
}

export function MultiLineInput({ value, onChange, onSubmit, theme }: MultiLineInputProps) {
  useInput((input, key) => {
    // 群复核 P1：先判 Ctrl+J 换行再判 Enter（Ink 可能把 LF 报成 return）
    if (key.ctrl && input === "j") {
      onChange(value + "\n");
      return;
    }
    if (key.return) {
      onSubmit(value);
      return;
    }
    if (key.backspace || key.delete) {
      onChange(value.slice(0, -1));
      return;
    }
    if (key.leftArrow || key.rightArrow || key.upArrow || key.downArrow) {
      return; // 光标/历史由 App 处理
    }
    if (key.tab || (key.shift && key.tab) || (key.ctrl && input === "c")) {
      return; // 补全/退出由 App 处理
    }
    if (input) {
      onChange(value + input);
    }
  });

  const lines = value.split("\n");
  const cursor = "█";
  return (
    <Text color={theme.colors.assistant}>
      {lines.map((line, i) => (
        <Text key={i}>
          {line}
          {i === lines.length - 1 ? (
            <Text color={theme.colors.prompt}>{cursor}</Text>
          ) : null}
          {i < lines.length - 1 ? "\n" : ""}
        </Text>
      ))}
    </Text>
  );
}
